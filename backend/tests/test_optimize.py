"""The optimisation workflow on the simulated board: one real speed-up is accepted, a bad variant is rejected."""
import json
import time

import pytest

from app.build.engine import SUCCESS as BUILD_SUCCESS
from app.build.engine import BuildResult, BuildStore
from app.pipeline.store import PipelineStore
from app.services.device import DeviceManager

from conftest import auth
from deploy_fakes import add_build, running, sha
from fake_llm import DEFAULT_REPORT, FakeLLM, call, say
from sim_board import SimFirmware, SimLink
from test_agents import use
from test_api_services import make_stock, wait_done

SLOW = ('#include "app_api.h"\n#include <stdio.h>\n#include <string.h>\n'
        'int app_command(const char *args, char *out, size_t cap) {\n'
        '    int a, b; char op[8];\n'
        '    if (sscanf(args, "%7s %d %d", op, &a, &b) != 3) return APP_ERR_ARGS;\n'
        '    if (strcmp(op, "MUL") == 0) { int p = 0; for (int i = 0; i < b; i++) p += a;'
        ' snprintf(out, cap, "product=%d", p); return APP_OK; }\n'
        '    return APP_ERR_ARGS;\n}\n')
FAST = SLOW.replace('int p = 0; for (int i = 0; i < b; i++) p += a; snprintf(out, cap, "product=%d", p);',
                    'snprintf(out, cap, "product=%d", a * b);')
SPEC = {"project": "calc", "commands": [{"name": "MUL", "usage": "APP MUL <a> <b>", "examples": [
    {"request": "APP MUL 2 3", "expect": {"kind": "equals", "value": "OK product=6"}, "why": ""}]}]}
PERF = {**DEFAULT_REPORT, "build_id": 1, "flash_bytes": 50000, "ram_bytes": 4000, "latency": [],
        "improvements": ["MUL adds in a loop: use a * b (O(1) instead of O(b))"]}
FW_REPORT = {**DEFAULT_REPORT, "files_written": ["app.c"], "commands": [], "scan_verdict": "PASS"}


class SizedBuilds:
    """The build engine stand-in: flash = 40 000 + 10 x the source length; the board runs each new build."""

    def __init__(self, services, board):
        self.services, self.board, self.calls = services, board, 0

    def __call__(self, name, timeout_s=None, cfg=None, on_stage=None, keep_tree=False):
        self.calls += 1
        files = {p.name: p.read_text() for p in (cfg.workspace_dir / name / "app").iterdir() if p.is_file()}
        nid = len(BuildStore(cfg.db_path).list(None, 100)) + 1
        size = 40000 + 10 * sum(len(v) for v in files.values())
        data, src = add_build(self.services, nid, seed=nid, project=name, files=files,
                              flash_bytes=size, ram_bytes=4000)
        self.board.outcomes[sha(data)] = running(nid, src)
        r = BuildStore(cfg.db_path).get(nid)["result"]
        return BuildResult(**{k: v for k, v in r.items() if k in BuildResult.__dataclass_fields__})


@pytest.fixture()
def rig(client, services, board, monkeypatch):
    """The board runs build #1 of calc (the slow MUL). Build #2 will be fast, unless the test says otherwise."""
    fw = SimFirmware(build_id=1, builds={1: set(), 2: {"mul_fast"}})
    services.device = DeviceManager(link_factory=lambda: SimLink(fw))
    for svc in (services.tests, services.analysis, services.fuzz, services.deploys, services.profiling):
        svc.device = services.device
    services.fuzz.recover_s = 0.3
    make_stock(services)
    services.firmware.create_project("calc")
    services.firmware.write_source("calc", "app.c", SLOW)
    data, src = add_build(services, 1, project="calc", files={"app.c": SLOW}, flash_bytes=40000 + 10 * len(SLOW),
                          ram_bytes=4000)
    board.outcomes[sha(data)] = running(1, src)
    store = PipelineStore(services.settings.db_path)
    p = store.create("calc", "t", {})
    store.update(p["id"], project="calc", spec_json=json.dumps(SPEC))
    monkeypatch.setattr("app.services.builds.build_project", SizedBuilds(services, board))
    real_flash = board.flash

    def flash(path, log):
        real_flash(path, log)
        if board.state.build_id:
            fw.flash(int(board.state.build_id), board.state.src_hash)
    board.flash = flash
    client.post("/tests/suites", headers=auth("agent"), json={"name": "calc-checks", "project": "calc", "cases": [
        {"name": "mul", "command": "APP MUL 2 3", "expect": {"equals": "OK product=6"}}]})
    return {"fw": fw}


def start(client, **kw):
    r = client.post("/optimizations", headers=auth("agent"), json={"project": "calc", **kw})
    assert r.status_code == 202, r.text
    return r.json()


def wait_opt(services, oid, statuses=("FINISHED", "FAILED", "STOPPED", "WAITING_APPROVAL"), timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        o = services.optimizations.get(oid)
        if o["status"] in statuses:
            time.sleep(0.1)
            return services.optimizations.get(oid)
        time.sleep(0.05)
    o = services.optimizations.get(oid)
    events = [(e["stage"], e["kind"], e["message"][:80]) for e in services.optimizations.store.events(oid)][-6:]
    jobs = [(j["id"], j["kind"], j["status"]) for j in services.jobs.list(8)]
    raise AssertionError(f"optimisation {oid} stuck in {o['stage']} ({o['status']}); last events {events}; jobs {jobs}")


def test_the_baseline_is_measured_on_the_board(client, services, rig):
    use(services, FakeLLM([]))
    services.optimizations.after_baseline = lambda o: "report"
    o = wait_opt(services, start(client)["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["base_build"] == 1 and o["benchmark"] == "bench-calc" and o["base_run_id"]
    run = services.profiling.run(o["base_run_id"])
    assert run["repeatable"] and run["result"]["commands"]["mul"]["device_us"]["median"] == 40
    assert set(o["state"]["base_suites"]) == {"calc-checks", "smoke"} and o["state"]["base_failing"] == []
    assert o["state"]["base_size"]["flash_bytes"] == 40000 + 10 * len(SLOW)


def test_the_board_must_run_the_project(client, services, rig):
    use(services, FakeLLM([]))
    rig["fw"].build_id = 99
    o = wait_opt(services, start(client)["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["outcome"] == "no_baseline" and "#99" in o["error"]


def perf_then_code(code=FAST, idea=None):
    """The Performance agent proposes, then the Firmware agent writes the variant."""
    return [say("analysed"), call("write_source", project="calc", filename="app.c", content=code), say("done")]


def test_the_performance_agent_proposes_and_the_firmware_agent_writes(client, services, rig):
    llm = use(services, FakeLLM(perf_then_code(), report=[PERF, FW_REPORT]))
    services.optimizations.after_variant = lambda o: "report"
    o = wait_opt(services, start(client)["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["error"] is None, o["error"]
    perf_task = llm.seen[0][1][0]["text"]
    assert "make 'mul' (APP MUL 2 3) faster on the device" in perf_task and "40.0 us on the device" in perf_task
    assert "run_benchmark" in llm.seen[0][2] and "write_source" not in llm.seen[0][2]
    fw_task = llm.seen[1][1][0]["text"]
    assert "use a * b" in fw_task and "byte-for-byte the same" in fw_task and "APP MUL <a> <b>" in fw_task
    assert o["state"]["target"] == "mul" and o["variants_tried"] == 1
    assert "+    if (strcmp(op, \"MUL\") == 0) { snprintf(out, cap, \"product=%d\", a * b);" in o["diff_text"]


def test_no_idea_means_no_variant(client, services, rig):
    use(services, FakeLLM([say("all fine")], report=[{**PERF, "improvements": []}]))
    o = wait_opt(services, start(client)["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["outcome"] == "no_idea" and o["variants_tried"] == 0


def approve(client, job_id):
    job = client.get(f"/jobs/{job_id}", headers=auth("viewer")).json()
    assert client.post(f"/jobs/{job_id}/approve", headers=auth("operator"),
                       json={"confirm": job["confirm_token"]}).status_code == 200


def resume(client, o):
    r = client.post(f"/optimizations/{o['id']}/resume", headers=auth("agent"))
    assert r.status_code == 202, r.text


def test_a_faster_variant_is_measured_after_approval(client, services, rig):
    use(services, FakeLLM(perf_then_code(), report=[PERF, FW_REPORT]))
    o = wait_opt(services, start(client)["id"])
    assert o["status"] == "WAITING_APPROVAL" and o["variant_build"] == 2 and rig["fw"].build_id == 1
    assert o["state"]["size"]["flash_bytes"]["change_pct"] < 0
    approve(client, o["deploy_job_id"])
    resume(client, o)
    o = wait_opt(services, o["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["outcome"] == "accepted", o["error"]
    assert o["state"]["check_problems"] == [] and o["state"]["check"]["calc-checks"]["verdict"] == "same"
    m = o["state"]["comparison"]["commands"]["mul"]["device_us"]
    assert (m["before"], m["after"], m["verdict"]) == (40.0, 12.0, "better")


def test_a_slower_variant_is_rejected_after_measuring(client, services, rig):
    rig["fw"].builds[2] = {"mul_slow"}
    use(services, FakeLLM(perf_then_code(), report=[PERF, FW_REPORT], ))
    o = wait_opt(services, start(client, max_variants=1)["id"])
    approve(client, o["deploy_job_id"])
    resume(client, o)
    o = wait_opt(services, o["id"])
    assert o["status"] == "WAITING_APPROVAL" and o["stage"] == "rolled_back"
    assert o["state"]["tried"][0]["why"].startswith("mul: 40.0 -> 400.0")
    assert services.firmware.read_source("calc", "app.c")["content"] == SLOW
    approve(client, o["rollback_job_id"])
    resume(client, o)
    o = wait_opt(services, o["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["outcome"] == "rejected_rolled_back" and rig["fw"].build_id == 1


def test_a_flash_goal_is_decided_without_the_board(client, services, rig):
    use(services, FakeLLM(perf_then_code(SLOW + "/* padding padding padding */\n"), report=[PERF, FW_REPORT]))
    o = wait_opt(services, start(client, goal="flash", max_variants=1)["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["outcome"] == "no_gain" and o["deploy_job_id"] is None
    assert "flash changed by" in o["state"]["tried"][0]["why"]
    assert services.firmware.read_source("calc", "app.c")["content"] == SLOW


def test_one_bad_variant_rejected_then_a_real_speedup_accepted(client, services, rig):
    """The completion test of the phase: variant 1 is slower (rejected, rolled back), variant 2 is faster (kept)."""
    rig["fw"].builds[2] = {"mul_slow"}
    rig["fw"].builds[3] = {"mul_fast"}
    llm = use(services, FakeLLM(perf_then_code(FAST) + perf_then_code(FAST + "/* v2 */"),
                                report=[PERF, FW_REPORT, PERF, FW_REPORT]))
    o = wait_opt(services, start(client, max_variants=2)["id"])
    approve(client, o["deploy_job_id"])                       # variant 1 (build 2)
    resume(client, o)
    o = wait_opt(services, o["id"])
    assert o["stage"] == "rolled_back"
    approve(client, o["rollback_job_id"])                     # back to build 1
    resume(client, o)
    deadline = time.time() + 20
    while services.optimizations.get(o["id"])["variant_build"] != 4 and time.time() < deadline:
        time.sleep(0.05)
    o = wait_opt(services, o["id"])
    assert o["stage"] == "deployed" and o["variants_tried"] == 2
    assert "REJECTED" in llm.seen[3][1][0]["text"] and "400.0" in llm.seen[3][1][0]["text"]
    approve(client, o["deploy_job_id"])                       # variant 2
    resume(client, o)
    o = wait_opt(services, o["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["outcome"] == "accepted" and rig["fw"].build_id == o["variant_build"]
    assert o["state"]["comparison"]["commands"]["mul"]["device_us"]["after"] == 12.0
    assert len(o["state"]["tried"]) == 1


def test_a_variant_that_breaks_a_check_is_rejected(client, services, rig):
    rig["fw"].builds[2] = {"mul_fast", "long_arg_crash"}        # faster, but the quick fuzz finds a crash
    use(services, FakeLLM(perf_then_code(), report=[PERF, FW_REPORT]))
    o = wait_opt(services, start(client, max_variants=1)["id"])
    approve(client, o["deploy_job_id"])
    resume(client, o)
    o = wait_opt(services, o["id"])
    assert o["stage"] == "rolled_back" and "crash" in o["state"]["tried"][0]["why"]
    client.post(f"/jobs/{o['rollback_job_id']}/reject", headers=auth("operator"), json={})
    resume(client, o)
    o = wait_opt(services, o["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert o["outcome"] == "rejected_not_rolled_back" and "still runs the rejected variant" in o["error"]


def test_the_report_api_and_rules(client, services, rig):
    use(services, FakeLLM(perf_then_code(), report=[PERF, FW_REPORT]))
    o = wait_opt(services, start(client, fuzz_check=False)["id"])
    assert client.post("/optimizations", headers=auth("agent"), json={"project": "calc"}).status_code == 409
    assert client.get(f"/optimizations/{o['id']}/report", headers=auth("viewer")).status_code == 409
    approve(client, o["deploy_job_id"])
    resume(client, o)
    o = wait_opt(services, o["id"], ("FINISHED", "FAILED", "STOPPED"))
    md = client.get(f"/optimizations/{o['id']}/report", headers=auth("viewer")).text
    assert md.startswith(f"# Optimisation {o['id']}: accepted")
    assert "| mul | 40.0 us | 12.0 us | -70.0 % |" in md and "```diff" in md and "approved by api:operator" in md
    rep = client.get(f"/optimizations/{o['id']}", headers=auth("viewer")).json()["report"]
    assert rep["outcome"] == "accepted" and rep["target"] == "mul" and rep["tokens"] > 0
    assert client.get("/optimizations?project=calc", headers=auth("viewer")).json()[0]["id"] == o["id"]
    assert client.post("/optimizations", headers=auth("viewer"), json={"project": "calc"}).status_code == 403
    assert client.post("/optimizations", headers=auth("agent"), json={"project": "nope"}).status_code == 404
    assert client.post("/optimizations", headers=auth("agent"),
                       json={"project": "calc", "goal": "beauty"}).status_code == 422


def test_the_optimisation_tools(client, services, rig):
    use(services, FakeLLM([say("nothing")], report=[{**PERF, "improvements": []}]))
    r = client.post("/tools/start_optimization/call", headers=auth("agent"), json={"arguments": {"project": "calc"}})
    assert r.status_code == 200, r.text
    o = wait_opt(services, r.json()["result"]["id"], ("FINISHED", "FAILED", "STOPPED"))
    got = client.post("/tools/get_optimization/call", headers=auth("viewer"),
                      json={"arguments": {"optimization_id": o["id"]}})
    assert got.json()["result"]["outcome"] == "no_idea"
    assert client.post("/tools/start_optimization/call", headers=auth("viewer"),
                       json={"arguments": {"project": "calc"}}).status_code == 403
