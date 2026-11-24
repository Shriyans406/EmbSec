"""The remediation workflow on the simulated board: a planted bug is found by the fuzzer, then fixed (or not)."""
import time

import pytest

from app.pipeline.store import PipelineStore
from app.services.device import DeviceManager

from conftest import auth
from deploy_fakes import add_build, running, sha
from fake_llm import DEFAULT_REPORT, FakeLLM, call, say
from sim_board import SimFirmware, SimLink
from test_agents import use
from test_api_services import make_stock, wait_done
from test_pipeline import Builds

BUGGY = ("#include \"app_api.h\"\n#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n"
         "int app_command(const char *args, char *out, size_t cap) {\n"
         "    int a = 0, b = 0;\n    char op[8];\n"
         "    if (sscanf(args, \"%7s %d %d\", op, &a, &b) != 3) return APP_ERR_ARGS;\n"
         "    if (strcmp(op, \"DIV\") == 0) { while (b == 0) { } snprintf(out, cap, \"quotient=%d\", a / b);"
         " return APP_OK; }\n"
         "    return APP_ERR_ARGS;\n}\n")
FIXED = BUGGY.replace("while (b == 0) { }", "if (b == 0) { snprintf(out, cap, \"division by zero\"); "
                                            "return APP_ERR_RANGE; }")
SPEC = {"project": "calc", "commands": [{"name": "DIV", "usage": "APP DIV <a> <b>",
                                         "examples": [{"request": "APP DIV 6 3"}]}]}
ROOT = {**DEFAULT_REPORT, "root_cause": "app.c line 9 loops forever when b is 0", "fix": "return APP_ERR_RANGE",
        "files_written": ["app.c"], "confidence": "high"}


@pytest.fixture()
def rig(client, services, board, monkeypatch):
    """Board runs build #1 of project calc, which hangs on APP DIV x 0. New builds have no bugs unless listed."""
    fw = SimFirmware(build_id=1, builds={1: {"div_zero_hang"}})
    services.device = DeviceManager(link_factory=lambda: SimLink(fw))
    for svc in (services.tests, services.analysis, services.fuzz, services.deploys):
        svc.device = services.device
    services.fuzz.recover_s = 0.3
    services.remediations.recover_s = 0.3
    services.remediations.sleep = lambda s: None
    make_stock(services)
    services.firmware.create_project("calc")
    services.firmware.write_source("calc", "app.c", BUGGY)
    data, src = add_build(services, 1, project="calc", files={"app.c": BUGGY})
    board.outcomes[sha(data)] = running(1, src)              # build #1 can be flashed back (rollback)
    store = PipelineStore(services.settings.db_path)
    p = store.create("calc", "t", {})
    store.update(p["id"], project="calc", spec_json=__import__("json").dumps(SPEC))
    builds = Builds(services, [], board)
    monkeypatch.setattr("app.services.builds.build_project", builds)
    real_flash = board.flash

    def flash(path, log):                     # the fake board flashes the simulated firmware too
        real_flash(path, log)
        if board.state.build_id:
            fw.flash(int(board.state.build_id), board.state.src_hash)
    board.flash = flash
    r = client.post("/fuzz/campaigns", headers=auth("agent"),
                    json={"project": "calc", "families": ["boundary"], "max_inputs": 200, "seed": 1})
    wait_done(client, r.json()["job_id"], 60)
    hang = next(f for f in services.fuzz.store.findings(campaign_id=r.json()["id"]) if f["kind"] == "hang")
    return {"fw": fw, "finding": hang, "builds": builds}


def start(client, fid, **kw):
    r = client.post("/remediations", headers=auth("agent"), json={"finding_id": fid, **kw})
    assert r.status_code == 202, r.text
    return r.json()


def wait_rem(services, rid, statuses=("FINISHED", "FAILED", "STOPPED", "WAITING_APPROVAL"), timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = services.remediations.get(rid)
        if r["status"] in statuses:
            time.sleep(0.1)
            return services.remediations.get(rid)
        time.sleep(0.05)
    raise AssertionError(f"remediation {rid} stuck in {services.remediations.get(rid)['stage']}")


def stages(services, rid):
    return [(e["stage"], e["kind"]) for e in services.remediations.store.events(rid)]


def test_reproduce_and_inspect_take_a_baseline(client, services, rig):
    use(services, FakeLLM([]))
    services.remediations.after_inspect = lambda r: "report"
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    assert rem["before_build"] == 1 and rem["project"] == "calc"
    assert rem["state"]["reproduction"]["hits"] == 3
    ev = rem["state"]["evidence"]
    assert ev["finding"]["kind"] == "hang" and ev["files"] == ["app.c"] and ev["scan"]["verdict"] == "PASS"
    assert set(rem["state"]["baseline"]) == {"smoke"} and rem["state"]["baseline"]["smoke"]["build_id"] == 1


def test_a_bug_that_does_not_happen_is_not_fixed(client, services, rig):
    use(services, FakeLLM([]))
    rig["fw"].bugs.clear()
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "not_reproduced" and "nothing to fix" in rem["error"]


def test_the_debugger_patches_the_code_and_the_diff_is_kept(client, services, rig):
    llm = use(services, FakeLLM([call("read_source", project="calc", filename="app.c"),
                                 call("write_source", project="calc", filename="app.c", content=FIXED),
                                 say("Root cause: app.c line 9 loops while b is 0. Fix: return APP_ERR_RANGE.")],
                                report=[ROOT]))
    services.remediations.after_fix = lambda r: "report"
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    task = llm.seen[0][1][0]["text"]
    assert "APP DIV 1 0" in task and "hang" in task and "build #1" in task
    assert "write_source" in llm.seen[0][2] and "start_build" not in llm.seen[0][2]
    assert rem["root_cause"] == ROOT["root_cause"]
    assert "-    if (strcmp(op, \"DIV\") == 0) { while (b == 0) { }" in rem["diff_text"]
    assert "+    if (strcmp(op, \"DIV\") == 0) { if (b == 0)" in rem["diff_text"]
    assert rem["state"]["diff_stats"] == {"files": ["app.c"], "added": 1, "removed": 1}


def test_no_change_means_no_fix(client, services, rig):
    use(services, FakeLLM([say("I could not find it.")], report=[ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "no_fix" and rem["diff_text"] == ""


def fixer(content=FIXED):
    return [call("write_source", project="calc", filename="app.c", content=content), say("Root cause: loop. Fix: check.")]


def approve(client, rem):
    job = client.get(f"/jobs/{rem['deploy_job_id']}", headers=auth("viewer")).json()
    assert client.post(f"/jobs/{job['id']}/approve", headers=auth("operator"),
                       json={"confirm": job["confirm_token"]}).status_code == 200


def resume(client, rem):
    r = client.post(f"/remediations/{rem['id']}/resume", headers=auth("agent"))
    assert r.status_code == 202, r.text


def test_the_fix_is_deployed_after_approval_and_the_bug_is_gone(client, services, rig, board):
    use(services, FakeLLM(fixer(), report=[ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    assert rem["status"] == "WAITING_APPROVAL" and rem["after_build"] == 2 and rig["fw"].build_id == 1
    assert client.post(f"/remediations/{rem['id']}/resume", headers=auth("agent")).status_code == 409
    approve(client, rem)
    resume(client, rem)
    rem = wait_rem(services, rem["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "fixed" and rig["fw"].build_id == 2
    assert [x["hits"] for x in rem["state"]["retest"]] == [0]


def test_a_fix_that_does_not_work_goes_back_to_the_debugger(client, services, rig, board):
    rig["fw"].builds[2] = {"div_zero_hang"}                   # the first "fix" still hangs on the board
    llm = use(services, FakeLLM(fixer(FIXED + "\n") + fixer(), report=[ROOT, ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    approve(client, rem)
    resume(client, rem)
    deadline = time.time() + 20
    while services.remediations.get(rem["id"])["after_build"] != 3 and time.time() < deadline:
        time.sleep(0.05)
    rem = wait_rem(services, rem["id"])
    assert rem["status"] == "WAITING_APPROVAL" and rem["fix_rounds"] == 1 and rem["after_build"] == 3
    assert "YOUR PREVIOUS FIX DID NOT WORK" in llm.seen[2][1][0]["text"] and "still fails 5 of 5" in llm.seen[2][1][0]["text"]
    approve(client, rem)
    resume(client, rem)
    rem = wait_rem(services, rem["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "fixed" and rig["fw"].build_id == 3


def test_a_rejected_fix_is_not_deployed(client, services, rig, board):
    use(services, FakeLLM(fixer(), report=[ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    client.post(f"/jobs/{rem['deploy_job_id']}/reject", headers=auth("operator"), json={"reason": "no"})
    resume(client, rem)
    rem = wait_rem(services, rem["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "not_deployed" and rig["fw"].build_id == 1


def _suite(client):
    cases = [{"name": "div", "command": "APP DIV 6 3", "expect": {"equals": "OK quotient=2"}},
             {"name": "mul", "command": "APP MUL 2 3", "expect": {"equals": "OK product=6"}}]
    assert client.post("/tests/suites", headers=auth("agent"),
                       json={"name": "calc-checks", "project": "calc", "cases": cases}).status_code == 201


def test_an_accepted_fix_marks_the_finding_fixed(client, services, rig, board):
    _suite(client)
    use(services, FakeLLM(fixer(), report=[ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    assert set(rem["state"]["baseline"]) == {"calc-checks", "smoke"}
    approve(client, rem)
    resume(client, rem)
    rem = wait_rem(services, rem["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "fixed" and rem["state"]["regressions"] == []
    assert rem["state"]["regression"]["calc-checks"]["verdict"] == "same"
    assert rem["state"]["fuzz_check"]["serious"] == []
    assert services.fuzz.finding(rig["finding"]["id"])["status"] == "fixed"
    r = client.post("/remediations", headers=auth("agent"), json={"finding_id": rig["finding"]["id"]})
    assert r.status_code == 409                                   # a fixed finding is closed


def test_a_fix_that_breaks_something_is_rejected_and_rolled_back(client, services, rig, board):
    _suite(client)
    rig["fw"].builds[2] = {"mul_wrong"}                       # the fix removes the hang but breaks MUL
    use(services, FakeLLM(fixer(), report=[ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    approve(client, rem)
    resume(client, rem)
    rem = wait_rem(services, rem["id"])
    assert rem["status"] == "WAITING_APPROVAL" and rem["stage"] == "rolled_back"
    assert rem["state"]["regressions"][0]["case"] == "mul"
    assert services.firmware.read_source("calc", "app.c")["content"] == BUGGY          # sources restored
    job = client.get(f"/jobs/{rem['rollback_job_id']}", headers=auth("viewer")).json()
    assert job["confirm_token"] == "1"
    assert client.post(f"/jobs/{job['id']}/approve", headers=auth("operator"), json={"confirm": "1"}).status_code == 200
    resume(client, rem)
    rem = wait_rem(services, rem["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "rejected_rolled_back" and rig["fw"].build_id == 1
    assert services.fuzz.finding(rig["finding"]["id"])["status"] != "fixed"


def test_a_new_crash_found_by_the_quick_fuzz_rejects_the_fix(client, services, rig, board):
    rig["fw"].builds[2] = {"long_arg_crash"}
    use(services, FakeLLM(fixer(), report=[ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"])["id"])
    approve(client, rem)
    resume(client, rem)
    rem = wait_rem(services, rem["id"])
    assert rem["stage"] == "rolled_back" and any("crash" in x for x in rem["state"]["rejection"])
    client.post(f"/jobs/{rem['rollback_job_id']}/reject", headers=auth("operator"), json={})
    resume(client, rem)
    rem = wait_rem(services, rem["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert rem["outcome"] == "rejected_not_rolled_back" and "still runs the rejected build" in rem["error"]


def test_the_report_and_the_api(client, services, rig, board):
    use(services, FakeLLM(fixer(), report=[ROOT]))
    rem = wait_rem(services, start(client, rig["finding"]["id"], fuzz_check=False)["id"])
    assert client.post("/remediations", headers=auth("agent"),
                       json={"finding_id": rig["finding"]["id"]}).status_code == 409       # one at a time
    assert client.get(f"/remediations/{rem['id']}/report", headers=auth("viewer")).status_code == 409
    approve(client, rem)
    resume(client, rem)
    rem = wait_rem(services, rem["id"], ("FINISHED", "FAILED", "STOPPED"))
    md = client.get(f"/remediations/{rem['id']}/report", headers=auth("viewer")).text
    assert md.startswith(f"# Remediation {rem['id']}: fixed") and "```diff" in md and "## Root cause" in md
    assert "`APP DIV 1 0`: failed 0 of 5 times" in md and "approved by api:operator" in md
    diff = client.get(f"/remediations/{rem['id']}/diff", headers=auth("viewer")).text
    assert diff.startswith("--- a/app.c")
    rep = client.get(f"/remediations/{rem['id']}", headers=auth("viewer")).json()["report"]
    assert rep["outcome"] == "fixed" and rep["fuzz_check"] is None and rep["tokens"] > 0
    assert client.get("/remediations", headers=auth("viewer")).json()[0]["id"] == rem["id"]
    events = client.get(f"/remediations/{rem['id']}/events", headers=auth("viewer")).json()["events"]
    assert events[0]["stage"] == "reproduce" and events[-1]["stage"] == "report"


def test_remediation_rules(client, services, rig):
    use(services, FakeLLM([]))
    assert client.post("/remediations", headers=auth("viewer"), json={"finding_id": 1}).status_code == 403
    assert client.post("/remediations", headers=auth("agent"), json={"finding_id": 999}).status_code == 404
    info = next((f for f in services.fuzz.store.findings() if f["severity"] == "info"), None)
    if info:
        assert client.post("/remediations", headers=auth("agent"), json={"finding_id": info["id"]}).status_code == 422
    services.agents._custom_llm = False
    r = client.post("/remediations", headers=auth("agent"), json={"finding_id": rig["finding"]["id"]})
    assert r.status_code == 503


def test_remediation_tools(client, services, rig):
    use(services, FakeLLM([say("nothing")], report=[ROOT]))
    r = client.post("/tools/start_remediation/call", headers=auth("agent"),
                    json={"arguments": {"finding_id": rig["finding"]["id"]}})
    assert r.status_code == 200
    rid = r.json()["result"]["id"]
    rem = wait_rem(services, rid, ("FINISHED", "FAILED", "STOPPED"))
    got = client.post("/tools/get_remediation/call", headers=auth("viewer"), json={"arguments": {"remediation_id": rid}})
    assert got.json()["result"]["outcome"] == "no_fix"
    assert client.post("/tools/start_remediation/call", headers=auth("viewer"),
                       json={"arguments": {"finding_id": 1}}).status_code == 403
