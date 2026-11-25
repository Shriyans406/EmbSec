"""The requirement-to-firmware pipeline, with a scripted model, fake builds, a fake board and the fake link."""
import time

import pytest

from app.build.engine import FAILED as BUILD_FAILED
from app.build.engine import SUCCESS as BUILD_SUCCESS
from app.build.engine import BuildResult
from app.pipeline.spec import check_spec, spec_cases, spec_for_coder

from conftest import FakeLink, auth
from deploy_fakes import add_build, running, sha
from fake_llm import DEFAULT_REPORT, FakeLLM, call, say
from test_agents import use
from test_api_services import make_stock

SPEC = {
    "project": "calc", "title": "calculator", "summary": "adds numbers",
    "commands": [{"name": "ADD", "usage": "APP ADD <a> <b>", "description": "adds", "reply": "sum=<a+b>",
                  "examples": [
                      {"request": "APP ADD 2 3", "expect": {"kind": "equals", "value": "OK sum=5"}, "why": "normal"},
                      {"request": "APP ADD 1", "expect": {"kind": "error", "value": "2"}, "why": "missing arg"},
                  ]}],
    "constraints": ["32-bit"], "assumptions": ["wraps on overflow"],
}

GOOD_C = "int app_command(const char *a, char *o, unsigned c) { return 0; }\n"
BAD_C = "void app_init(void) { gpio_put(13, 1); }\nint app_command(const char *a, char *o, unsigned c) { return 0; }\n"


class Builds:
    """Stands in for the build engine: each call 'builds' the project's current sources."""

    def __init__(self, services, outcomes, board=None):
        self.services = services
        self.outcomes = list(outcomes)          # BUILD_SUCCESS / BUILD_FAILED per build, in order
        self.calls = 0
        self.board = board                      # if given, the board will come up running each new build

    def __call__(self, name, timeout_s=None, cfg=None, on_stage=None, keep_tree=False):
        self.calls += 1
        status = self.outcomes.pop(0) if self.outcomes else BUILD_SUCCESS
        app = cfg.workspace_dir / name / "app"
        files = {p.name: p.read_text() for p in app.iterdir() if p.is_file()}
        from app.build.engine import BuildStore

        next_id = len(BuildStore(cfg.db_path).list(None, 100)) + 1
        data, src = add_build(self.services, next_id, seed=next_id, status=status,
                              project=name, files=files,
                              diagnostics=[] if status == BUILD_SUCCESS else [
                                  {"severity": "error", "message": "expected ';'", "file": "app/app.c", "line": 3,
                                   "column": 1, "flag": None, "source": "app", "category": "compile",
                                   "context": []}])
        if self.board is not None:
            self.board.outcomes[sha(data)] = running(next_id, src)
        r = BuildStore(cfg.db_path).get(next_id)["result"]
        return BuildResult(**{k: v for k, v in r.items() if k in BuildResult.__dataclass_fields__})


def coder(content=GOOD_C):
    """The firmware agent's script: write app.c, then answer."""
    return [call("write_source", project="calc", filename="app.c", content=content), say("written")]


def start(client, requirement="a calculator that adds two numbers", **kw):
    r = client.post("/pipelines", headers=auth("agent"), json={"requirement": requirement, **kw})
    assert r.status_code == 202, r.text
    return r.json()


def wait_pipe(services, pid, statuses=("FINISHED", "FAILED", "STOPPED", "WAITING_APPROVAL"), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        p = services.pipelines.get(pid)
        if p["status"] in statuses:
            time.sleep(0.1)
            return services.pipelines.get(pid)
        time.sleep(0.05)
    raise AssertionError(f"pipeline {pid} stuck in {services.pipelines.get(pid)['stage']}")


def stages(services, pid):
    return [(e["stage"], e["kind"]) for e in services.pipelines.store.events(pid)]


@pytest.fixture()
def builds(services, monkeypatch, board):
    b = Builds(services, [], board)
    monkeypatch.setattr("app.services.builds.build_project", b)
    make_stock(services)
    return b


def board_runs(build_id, add_ok=True):
    FakeLink.replies["VERSION"] = f"OK VERSION fw=0.3.0 proto=1 sdk=1.5.1 build={build_id} src=abcd1234"
    FakeLink.replies["APP ADD 2 3"] = "OK sum=5" if add_ok else "OK sum=6"
    FakeLink.replies["APP ADD 1"] = "ERR 2 BAD_ARGS usage: ADD a b"


def approve(client, p):
    job = client.get(f"/jobs/{p['deploy_job_id']}", headers=auth("viewer")).json()
    r = client.post(f"/jobs/{job['id']}/approve", headers=auth("operator"), json={"confirm": job["confirm_token"]})
    assert r.status_code == 200, r.text


def resume(client, p):
    r = client.post(f"/pipelines/{p['id']}/resume", headers=auth("agent"))
    assert r.status_code == 202, r.text


# ------------------------------------------------------------------ the spec
def test_spec_checks():
    clean, problems = check_spec(SPEC)
    assert problems == [] and clean["project"] == "calc"
    bad = {**SPEC, "project": "Bad Name", "commands": [{**SPEC["commands"][0], "examples": [
        {"request": "BOOTSEL", "expect": {"kind": "equals", "value": "x"}, "why": ""},
        {"request": "APP X", "expect": {"kind": "error", "value": "42"}, "why": ""},
        {"request": "APP Y", "expect": {"kind": "regex", "value": "("}, "why": ""},
        {"request": "APP é", "expect": {"kind": "equals", "value": "x"}, "why": ""}]}]}
    _, problems = check_spec(bad)
    text = " | ".join(problems)
    for part in ("project name", "BOOTSEL", "error code '42'", "bad regex", "printable ASCII"):
        assert part in text, part
    assert check_spec({**SPEC, "commands": []})[1] == ["there are no commands"]
    one = {**SPEC, "commands": [{**SPEC["commands"][0], "examples": SPEC["commands"][0]["examples"][:1]}]}
    assert "at least two examples" in check_spec(one)[1][0]


def test_spec_becomes_tests_and_a_coding_task():
    cases = spec_cases(SPEC)
    assert cases[0]["command"] == "PING"
    assert cases[1] == {"name": "ADD #1", "command": "APP ADD 2 3", "expect": {"equals": "OK sum=5"}}
    assert cases[2]["expect"] == {"prefix": "ERR 2 "}
    task = spec_for_coder(SPEC)
    assert "APP ADD <a> <b>" in task and "'APP ADD 2 3' -> equals 'OK sum=5'" in task and "wraps on overflow" in task


# ------------------------------------------------------------------ up to the scan
def test_spec_code_build_scan(client, services, builds):
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client, deploy=False)["id"])
    assert p["status"] == "FINISHED" and p["outcome"] == "built" and p["build_id"] == 1
    assert p["project"] == "calc" and p["spec"]["title"] == "calculator"
    assert [s for s, k in stages(services, p["id"]) if k == "done"] == ["spec", "code", "build", "scan", "report"]
    assert services.firmware.read_source("calc", "app.c")["content"] == GOOD_C


def test_a_failed_build_goes_back_to_the_coder_with_the_errors(client, services, builds):
    builds.outcomes = [BUILD_FAILED, BUILD_SUCCESS]
    llm = use(services, FakeLLM(coder() + coder(), report=[SPEC, DEFAULT_REPORT, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client, deploy=False)["id"])
    assert p["outcome"] == "built" and p["fix_rounds"] == 1 and p["build_id"] == 2
    second_task = llm.seen[2][1][0]["text"]
    assert "FAILED" in second_task and "app/app.c:3: expected ';'" in second_task
    assert [b["status"] for b in p["state"]["builds"]] == ["FAILED", "SUCCESS"]


def test_fix_rounds_are_limited(client, services, builds):
    builds.outcomes = [BUILD_FAILED] * 5
    use(services, FakeLLM(coder() * 3, report=[SPEC] + [DEFAULT_REPORT] * 5))
    p = wait_pipe(services, start(client, max_fix_rounds=1)["id"])
    assert p["status"] == "FAILED" and p["outcome"] == "failed" and "1 fix rounds" in p["error"]
    assert builds.calls == 2


def test_a_scan_failure_is_fixed_before_anything_else(client, services, builds):
    llm = use(services, FakeLLM(coder(BAD_C) + coder(GOOD_C), report=[SPEC, DEFAULT_REPORT, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client, deploy=False)["id"])
    assert p["outcome"] == "built" and p["fix_rounds"] == 1 and p["build_id"] == 2
    assert "SEC001" in llm.seen[2][1][0]["text"]


def test_an_unusable_spec_stops_the_pipeline(client, services, builds):
    bad = {**SPEC, "commands": []}
    use(services, FakeLLM([], report=[bad, bad]))
    p = wait_pipe(services, start(client)["id"])
    assert p["status"] == "FAILED" and "specification" in p["error"] and builds.calls == 0


# ------------------------------------------------------------------ deployment and tests on the board
def test_the_whole_way_with_a_human_approval(client, services, builds, board):
    board_runs(1)
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"])
    assert p["status"] == "WAITING_APPROVAL" and p["stage"] == "deployed" and p["deploy_job_id"]
    assert board.flashed == []
    r = client.post(f"/pipelines/{p['id']}/resume", headers=auth("agent"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "still_awaiting_approval"
    approve(client, p)
    resume(client, p)
    p = wait_pipe(services, p["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert p["status"] == "FINISHED" and p["outcome"] == "success", p["error"]
    assert len(board.flashed) == 1 and p["test_run_id"]
    run = client.get(f"/tests/runs/{p['test_run_id']}", headers=auth("viewer")).json()
    assert run["passed"] == run["total"] == 3                    # PING + the 2 examples
    assert "APP ADD 2 3" in FakeLink.sent and "VERSION" in FakeLink.sent


def test_a_rejected_deployment_ends_the_pipeline_without_tests(client, services, builds, board):
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"])
    client.post(f"/jobs/{p['deploy_job_id']}/reject", headers=auth("operator"), json={"reason": "not now"})
    resume(client, p)
    p = wait_pipe(services, p["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert p["outcome"] == "not_deployed" and "REJECTED" in p["error"] and p["test_run_id"] is None
    assert board.flashed == []


def test_no_board_means_no_proposal(client, services, builds, board):
    from app.deploy.board import ABSENT, BoardState

    board.state = BoardState(ABSENT)
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert p["outcome"] == "not_deployed" and "no board found on USB" in p["error"] and p["build_id"] == 1


def test_failing_checks_go_back_to_the_coder_and_need_a_new_approval(client, services, builds, board):
    board_runs(1, add_ok=False)
    llm = use(services, FakeLLM(coder() + coder(), report=[SPEC, DEFAULT_REPORT, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"])
    approve(client, p)
    resume(client, p)
    p = wait_pipe(services, p["id"], ("WAITING_APPROVAL", "FINISHED", "FAILED"))
    deadline = time.time() + 10
    while p["build_id"] != 2 and time.time() < deadline:      # the new build is being proposed
        time.sleep(0.05)
        p = services.pipelines.get(p["id"])
    p = wait_pipe(services, p["id"])
    assert p["status"] == "WAITING_APPROVAL" and p["test_rounds"] == 1 and p["build_id"] == 2
    task = llm.seen[2][1][0]["text"]
    assert "'APP ADD 2 3': got 'OK sum=6'" in task
    board_runs(2)
    approve(client, p)
    resume(client, p)
    p = wait_pipe(services, p["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert p["outcome"] == "success" and len(board.flashed) == 2


def test_checks_that_keep_failing_end_as_tests_failed(client, services, builds, board):
    board_runs(1, add_ok=False)
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, client.post("/pipelines", headers=auth("agent"),
                                        json={"requirement": "a calculator that adds", "max_test_rounds": 0})
                  .json()["id"])
    approve(client, p)
    resume(client, p)
    p = wait_pipe(services, p["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert p["outcome"] == "tests_failed" and p["status"] == "FINISHED"


def test_stopping_a_waiting_pipeline(client, services, builds, board):
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"])
    r = client.post(f"/pipelines/{p['id']}/stop", headers=auth("agent"))
    assert r.status_code == 200 and r.json()["status"] == "STOPPED" and r.json()["outcome"] == "stopped"
    assert client.post(f"/pipelines/{p['id']}/resume", headers=auth("agent")).status_code == 409


def test_the_report_is_built_from_the_records(client, services, builds, board):
    board_runs(1)
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"])
    approve(client, p)
    resume(client, p)
    p = wait_pipe(services, p["id"], ("FINISHED", "FAILED", "STOPPED"))
    r = client.get(f"/pipelines/{p['id']}", headers=auth("viewer")).json()["report"]
    assert r["outcome"] == "success" and r["final_build"]["id"] == 1 and r["deployment"]["status"] == "SUCCEEDED"
    assert r["tests"]["passed"] == 3 and r["spec"]["examples"] == 2 and r["tokens"]["total"] > 0
    assert [c["passed"] for c in r["checks"]] == [True] * 3
    md = client.get(f"/pipelines/{p['id']}/report", headers=auth("viewer")).text
    assert md.startswith(f"# Pipeline {p['id']}: success") and "| ADD #1 | `APP ADD 2 3` | `OK sum=5` | PASS |" in md
    assert "approved by api:operator" in md
    events = client.get(f"/pipelines/{p['id']}/events", headers=auth("viewer")).json()["events"]
    assert events[0]["stage"] == "spec" and events[-1]["stage"] == "report"


def test_pipeline_api_rules(client, services, builds):
    assert client.post("/pipelines", headers=auth("viewer"), json={"requirement": "a calculator please"}).status_code == 403
    assert client.post("/pipelines", headers=auth("agent"), json={"requirement": "short"}).status_code == 422
    assert client.get("/pipelines/99", headers=auth("viewer")).status_code == 404
    services.agents._custom_llm = False
    r = client.post("/pipelines", headers=auth("agent"), json={"requirement": "a calculator please"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ai_not_configured"


def test_a_model_failure_while_coding_stops_before_the_build(client, services, builds):
    from fake_llm import failing

    use(services, FakeLLM([failing("429 RESOURCE_EXHAUSTED quota")], report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert p["outcome"] == "failed" and "could not work" in p["error"] and "429" in p["error"]
    assert builds.calls == 0
