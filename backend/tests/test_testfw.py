"""The test framework: stored suites, richer checks, retries, regression across builds, coverage, tools."""
import json
import time

from app.pipeline.store import PipelineStore
from app.services.container import build_services
from app.services.testing import Expect, evaluate
from app.testfw.coverage import expected_error, suite_coverage

from conftest import FakeLink, auth
from test_api_services import wait_done
from test_pipeline import SPEC

PING = {"name": "ping", "command": "PING", "expect": {"equals": "OK PONG"}}
ADD = {"name": "add", "command": "APP ADD 2 3", "expect": {"equals": "OK sum=5"}}
LED = {"name": "led", "command": "LED ON", "expect": {"prefix": "OK LED"}}


def make(client, name="calc-checks", cases=None, role="agent", **kw):
    return client.post("/tests/suites", headers=auth(role),
                       json={"name": name, "cases": cases or [PING, ADD, LED], **kw})


def on_build(build, add="OK sum=5", led="OK LED state=ON"):
    FakeLink.replies["VERSION"] = f"OK VERSION fw=0.3.0 proto=1 sdk=1.5.1 build={build} src=ab{build:06d}"
    FakeLink.replies["APP ADD 2 3"] = add
    FakeLink.replies["LED ON"] = led


def run(client, suite="calc-checks", **body):
    r = client.post(f"/tests/suites/{suite}/run", headers=auth("agent"), json=body)
    assert r.status_code == 202, r.text
    return wait_done(client, r.json()["id"])


# ------------------------------------------------------------------ suites
def test_suite_lifecycle(client):
    r = make(client, project="calc", description="calculator checks")
    assert r.status_code == 201 and r.json()["revision"] == 1 and len(r.json()["cases"]) == 3
    assert make(client).status_code == 409
    assert make(client, role="viewer").status_code == 403
    got = client.get("/tests/suites/calc-checks", headers=auth("viewer")).json()
    assert got["project"] == "calc" and [c["name"] for c in got["cases"]] == ["ping", "add", "led"]
    listing = client.get("/tests/suites", headers=auth("viewer")).json()
    assert listing["calc-checks"]["cases"] == 3 and listing["smoke"]["builtin"] is True
    r = client.post("/tests/suites/calc-checks/cases", headers=auth("agent"),
                    json={"cases": [{"name": "mul", "command": "APP MUL 2 3", "expect": {"equals": "OK product=6"}}]})
    assert r.status_code == 200 and r.json()["revision"] == 2 and len(r.json()["cases"]) == 4
    assert client.post("/tests/suites/calc-checks/cases", headers=auth("agent"),
                       json={"cases": [PING]}).status_code == 409
    r = client.patch("/tests/suites/calc-checks/cases/led", headers=auth("agent"), json={"enabled": False})
    assert r.json()["revision"] == 3 and [c["enabled"] for c in r.json()["cases"]] == [True, True, False, True]
    assert client.delete("/tests/suites/calc-checks/cases/mul", headers=auth("agent")).json()["revision"] == 4
    assert client.delete("/tests/suites/calc-checks/cases/nope", headers=auth("agent")).status_code == 404
    assert make(client, cases=[PING], replace=True).json()["revision"] == 5
    assert client.delete("/tests/suites/calc-checks", headers=auth("agent")).status_code == 403
    assert client.delete("/tests/suites/calc-checks", headers=auth("operator")).json() == {"deleted": "calc-checks"}
    assert client.get("/tests/suites/calc-checks", headers=auth("viewer")).status_code == 404


def test_bad_suites_are_refused(client):
    assert make(client, name="Bad Name").status_code == 422
    r = make(client, name="bad", cases=[PING, {**ADD, "name": "ping"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_case"
    assert make(client, name="danger", cases=[{"name": "x", "command": "BOOTSEL", "expect": {"prefix": "OK"}}]) \
        .status_code in (403, 422)
    assert make(client, name="nothing", cases=[{"name": "x", "command": "PING", "expect": {}}]).status_code == 422


def test_builtin_suites_cannot_be_changed(client, services):
    assert make(client, name="smoke", cases=[PING], replace=True).status_code == 403
    assert client.post("/tests/suites/smoke/cases", headers=auth("agent"), json={"cases": [ADD]}).status_code == 403
    assert client.delete("/tests/suites/smoke", headers=auth("operator")).status_code == 403
    rev = services.tests.suites_db.get("smoke")["revision"]
    build_services(services.settings)                       # seeding again changes nothing
    assert services.tests.suites_db.get("smoke")["revision"] == rev


def test_sync_only_bumps_the_revision_when_cases_change(services):
    db = services.tests.suites_db
    assert db.sync("spec-calc", [PING, ADD], "t")["revision"] == 1
    assert db.sync("spec-calc", [PING, ADD], "t")["revision"] == 1
    assert db.sync("spec-calc", [PING], "t")["revision"] == 2


def test_disabled_cases_are_not_run_and_the_build_is_recorded(client):
    on_build(7)
    make(client)
    client.patch("/tests/suites/calc-checks/cases/led", headers=auth("agent"), json={"enabled": False})
    job = run(client)
    assert job["status"] == "SUCCEEDED" and job["result"]["total"] == 2
    assert job["result"]["firmware"] == {"fw_version": "0.3.0", "build_id": 7, "src_hash": "ab000007"}
    runs = client.get("/tests/runs", headers=auth("viewer")).json()
    assert runs[0]["build_id"] == 7 and runs[0]["suite_revision"] == 2 and runs[0]["duration_ms"] >= 0


# ------------------------------------------------------------------ richer checks
def test_field_number_and_latency_checks():
    r = "OK STATUS uptime_ms=5000 led=OFF errors=0"
    assert evaluate(r, {"field": "uptime_ms", "min": 1000, "max": 9000}) == (True, "")
    assert "above" in evaluate(r, {"field": "uptime_ms", "max": 10})[1]
    assert "below" in evaluate(r, {"field": "uptime_ms", "min": 6000})[1]
    assert evaluate(r, {"field": "led", "field_equals": "OFF"})[0] is True
    assert "no field" in evaluate(r, {"field": "temp", "min": 1})[1]
    assert "not a number" in evaluate(r, {"field": "led", "min": 1})[1]
    assert evaluate(r, {"not_contains": "ERR"})[0] and not evaluate(r, {"not_contains": "led"})[0]
    assert "took" in evaluate(r, {"prefix": "OK", "max_latency_ms": 5}, latency_ms=8.0)[1]
    assert evaluate(r, {"prefix": "OK", "max_latency_ms": 5}, latency_ms=2.0)[0]
    for bad in ({"min": 1}, {"field": "x"}, {"field": "x", "min": 5, "max": 1}):
        try:
            Expect(**bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


def test_retries_mark_flaky_checks(client):
    FakeLink.replies["APP ADD 2 3"] = ["OK sum=0", "OK sum=5"]          # wrong once, then right
    cases = [{**ADD, "retries": 2}, PING]
    job = wait_done(client, client.post("/tests/run", headers=auth("agent"), json={"cases": cases}).json()["id"])
    add = job["result"]["cases"][0]
    assert job["status"] == "SUCCEEDED" and add["attempts"] == 2 and add["flaky"] is True
    assert job["result"]["flaky"] == 1 and FakeLink.sent.count("APP ADD 2 3") == 2


def test_wait_before_a_check(client):
    t0 = time.time()
    wait_done(client, client.post("/tests/run", headers=auth("agent"),
                                  json={"cases": [{**PING, "wait_ms": 400}]}).json()["id"])
    assert time.time() - t0 >= 0.4


# ------------------------------------------------------------------ regression
def test_regression_across_builds(client):
    make(client)
    on_build(3, add="OK sum=6")
    first = run(client)
    on_build(4)
    second = run(client)
    on_build(5, led="ERR 2 BAD_ARGS")
    run(client)
    r = client.get("/tests/suites/calc-checks/regression", headers=auth("viewer")).json()
    assert r["verdict"] == "regression" and (r["baseline"]["build_id"], r["current"]["build_id"]) == (4, 5)
    assert [x["name"] for x in r["regressed"]] == ["led"] and r["regressed"][0]["after"] == "ERR 2 BAD_ARGS"
    r = client.get("/tests/suites/calc-checks/regression?build_id=4", headers=auth("viewer")).json()
    assert r["verdict"] == "improved" and [x["name"] for x in r["fixed"]] == ["add"]
    r = client.get("/tests/suites/calc-checks/regression?build_id=5&baseline_build=3", headers=auth("viewer")).json()
    assert r["counts"]["fixed"] == 1 and r["counts"]["regressed"] == 1
    assert client.get("/tests/suites/calc-checks/regression?build_id=3", headers=auth("viewer")).json()["verdict"] \
        == "no_baseline"
    assert client.get("/tests/suites/calc-checks/regression?build_id=9", headers=auth("viewer")).status_code == 404
    hist = client.get("/tests/suites/calc-checks/history", headers=auth("viewer")).json()["builds"]
    assert [(h["build_id"], h["failed"]) for h in hist] == [(5, 1), (4, 0), (3, 1)]
    cmp = client.get(f"/tests/compare?baseline={first['result']['run_id']}&current={second['result']['run_id']}",
                     headers=auth("viewer")).json()
    assert cmp["verdict"] == "improved" and cmp["notes"] == []


def test_comparing_runs_of_a_changed_suite_says_so(client):
    make(client)
    on_build(3)
    a = run(client)
    client.post("/tests/suites/calc-checks/cases", headers=auth("agent"),
                json={"cases": [{"name": "echo", "command": "ECHO hi", "expect": {"equals": "OK hi"}}]})
    FakeLink.replies["ECHO hi"] = "OK hi"
    b = run(client)
    cmp = client.get(f"/tests/compare?baseline={a['result']['run_id']}&current={b['result']['run_id']}",
                     headers=auth("viewer")).json()
    assert cmp["counts"]["added"] == 1 and any("revision" in n for n in cmp["notes"])
    assert any("same build" in n for n in cmp["notes"])


# ------------------------------------------------------------------ coverage
def test_coverage_of_the_smoke_suite(client):
    cov = client.get("/tests/suites/smoke/coverage", headers=auth("viewer")).json()
    assert {"PING", "HELLO", "LED"} <= set(cov["protocol"]["covered"]) and "TEMP" in cov["protocol"]["missing"]
    assert cov["error_codes"]["expected"] == {"UNKNOWN_CMD": 1, "BAD_ARGS": 2, "BAD_RANGE": 1}
    assert cov["balance"]["expect_error"] == 4 and cov["last_run"] is None


def test_coverage_against_the_projects_spec(client, services):
    store = PipelineStore(services.settings.db_path)
    p = store.create("a calculator", "t", {})
    store.update(p["id"], project="calc", spec_json=json.dumps(SPEC))
    make(client, project="calc")
    on_build(2)
    run(client)
    cov = client.get("/tests/suites/calc-checks/coverage", headers=auth("viewer")).json()
    assert cov["project"] == "calc" and cov["spec"]["commands_covered"] == ["ADD"]
    assert cov["spec"]["examples"] == 2 and cov["spec"]["examples_covered"] == 1 and cov["spec"]["percent"] == 50.0
    assert cov["spec"]["examples_missing"] == [{"command": "ADD", "request": "APP ADD 1"}]
    assert cov["last_run"]["build_id"] == 2 and cov["last_run"]["not_run"] == []


def test_coverage_helpers():
    assert expected_error({"prefix": "ERR 2 "}) == 2 and expected_error({"regex": "^ERR 3"}) == 3
    assert expected_error({"equals": "OK"}) is None
    cov = suite_coverage([{"name": "a", "command": "APP X 1", "expect": {"field": "v", "min": 1}, "enabled": True},
                          {"name": "b", "command": "APP Y", "expect": {"prefix": "ERR 2 "}, "enabled": False}])
    assert cov["enabled"] == 1 and cov["app_subcommands"] == {"X": 1} and cov["balance"]["other"] == 1


# ------------------------------------------------------------------ the tools
def call(client, name, args, role="agent"):
    return client.post(f"/tools/{name}/call", headers=auth(role), json={"arguments": args})


def test_the_suite_tools(client):
    r = call(client, "save_suite", {"name": "verify-calc", "project": "calc", "cases": [PING, ADD]})
    assert r.status_code == 200 and r.json()["result"]["revision"] == 1
    assert call(client, "save_suite", {"name": "smoke", "cases": [PING], "replace": True}).status_code == 403
    assert call(client, "save_suite", {"name": "verify-calc", "cases": [PING]}).status_code == 409
    call(client, "add_test_cases", {"suite": "verify-calc", "cases": [LED]})
    names = [s["name"] for s in call(client, "list_suites", {}, "viewer").json()["result"]]
    assert {"smoke", "verify-calc"} <= set(names)
    on_build(1)
    job = call(client, "run_suite", {"suite": "verify-calc"}).json()["result"]
    wait_done(client, job["id"])
    on_build(2, add="OK sum=4")
    wait_done(client, call(client, "run_suite", {"suite": "verify-calc"}).json()["result"]["id"])
    reg = call(client, "suite_regression", {"suite": "verify-calc"}, "viewer").json()["result"]
    assert reg["verdict"] == "regression" and reg["regressed"][0]["name"] == "add"
    cov = call(client, "suite_coverage", {"suite": "verify-calc"}, "viewer").json()["result"]
    assert cov["app_subcommands"] == {"ADD": 1}
    assert call(client, "run_suite", {"suite": "verify-calc"}, "viewer").status_code == 403
