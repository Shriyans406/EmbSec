"""The agent loop, run with a scripted model (no network, no board)."""
import base64
import time

import pytest

from app.ai import prompts
from app.ai.graph import AgentSpec
from app.ai.llm import GeminiLLM, LLMError, UsageLog, _retryable
from app.ai.runs import RunStore
from app.services.container import build_services

from conftest import auth, make_settings
from deploy_fakes import sha
from fake_llm import FakeLLM, call, calls, failing, say
from test_api_services import prepare_deploy


def use(services, llm):
    services.agents._llm = llm
    services.agents._custom_llm = True
    llm.usage_log = services.agents.usage
    return llm


def wait_run(services, run_id, statuses=("FINISHED", "FAILED", "STOPPED", "WAITING_APPROVAL"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        run = services.agents.get(run_id)
        if run["status"] in statuses:
            # the job that drives the run finishes right after the run row is updated
            for j in services.jobs.list(5, kind=None):
                if j["kind"].startswith("agent") and j["params"].get("run_id") == run_id and j["status"] == "RUNNING":
                    time.sleep(0.05)
            return services.agents.get(run_id)
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not settle: {services.agents.get(run_id)['status']}")


def start(client, goal="look at the projects", **kw):
    r = client.post("/agent/runs", headers=auth("agent"), json={"goal": goal, **kw})
    assert r.status_code == 202, r.text
    return r.json()


# ------------------------------------------------------------------ the loop
def test_a_run_uses_tools_and_ends_with_a_report(client, services):
    llm = use(services, FakeLLM([call("create_project", name="demo"),
                                 call("read_source", project="demo", filename="app.c"),
                                 say("the project has one file")]))
    run = wait_run(services, start(client)["id"])
    assert run["status"] == "FINISHED" and run["outcome"] == "success"
    assert run["steps"] == 2 and run["tokens"] == 300 and run["answer"] == "the project has one file"
    assert [p["name"] for p in run["prompts"]] == ["lab_agent", "platform", "run_report"]
    kinds = [(s["kind"], s["name"]) for s in services.agents.store.steps(run["id"])]
    assert kinds == [("model", "reply"), ("tool", "create_project"), ("model", "reply"), ("tool", "read_source"),
                     ("model", "reply"), ("report", "success")]
    # the second model call saw the result of the first tool
    tool_msg = llm.seen[1][1][-1]
    assert tool_msg["role"] == "tool" and tool_msg["results"][0]["response"]["result"]["name"] == "demo"
    usage = services.agents.usage.totals(run["id"])
    assert usage["calls"] == 4 and usage["total"] == 350
    assert "GOAL: look at the projects" in llm.json_seen[0][1][0]["text"]


def test_the_agent_acts_with_the_agent_role_and_sees_no_approval_tool(client, services):
    llm = use(services, FakeLLM([say("nothing to do")]))
    wait_run(services, start(client)["id"])
    names = llm.seen[0][2]
    assert len(names) == 40 and "propose_deployment" in names
    assert not [n for n in names if "approve" in n or "promote" in n]
    rows = client.get("/audit?limit=50", headers=auth("operator")).json()
    assert all(r["role"] == "agent" for r in rows if r["action"].startswith("tool."))


def test_tool_errors_go_back_to_the_model_as_data(client, services):
    llm = use(services, FakeLLM([call("read_source", project="nosuch", filename="app.c"),
                                 call("start_build", timeout_s=5), say("gave up")]))
    run = wait_run(services, start(client)["id"])
    assert run["status"] == "FINISHED"
    first = llm.seen[1][1][-1]["results"][0]["response"]
    second = llm.seen[2][1][-1]["results"][0]["response"]
    assert first["error"]["code"] == "not_found"
    assert second["error"]["code"] == "invalid_arguments" and second["error"]["details"]
    steps = [s for s in services.agents.store.steps(run["id"]) if s["kind"] == "tool"]
    assert [s["ok"] for s in steps] == [False, False]


def test_a_restricted_agent_only_gets_its_tools(client, services):
    services.agents.register(AgentSpec("reader", "lab_agent", ["list_projects", "read_source"]))
    llm = use(services, FakeLLM([calls(("list_projects", {}), ("write_source", {"project": "demo", "filename": "a.c",
                                                                               "content": "int x;"})),
                                 say("done")]))
    run = wait_run(services, start(client, agent="reader")["id"])
    assert sorted(llm.seen[0][2]) == ["list_projects", "read_source"]
    results = llm.seen[1][1][-1]["results"]
    assert "result" in results[0]["response"]
    assert results[1]["response"]["error"]["code"] == "tool_not_allowed"
    assert run["steps"] == 2
    assert client.get("/projects", headers=auth("viewer")).json() == []        # nothing was written


def test_calls_to_tools_that_do_not_exist_are_refused(client, services):
    llm = use(services, FakeLLM([call("approve_job", job_id=1), call("promote_known_good", build_id=1), say("ok")]))
    wait_run(services, start(client)["id"])
    for i in (1, 2):
        assert llm.seen[i][1][-1]["results"][0]["response"]["error"]["code"] == "tool_not_allowed"


def test_step_limit_makes_the_agent_wrap_up(client, services):
    llm = use(services, FakeLLM([call("list_projects")] * 2 + [say("summary after the limit")]))
    run = wait_run(services, start(client, max_steps=2)["id"])
    assert run["status"] == "FINISHED" and run["steps"] == 2
    system, messages, tool_names = llm.seen[-1]
    assert tool_names == [] and "step limit" in messages[-1]["text"]
    assert run["answer"] == "summary after the limit"


def test_two_calls_in_one_turn_past_the_limit(client, services):
    llm = use(services, FakeLLM([calls(("list_projects", {}), ("list_projects", {}), ("list_projects", {})),
                                 say("done")]))
    run = wait_run(services, start(client, max_steps=2)["id"])
    sent = llm.seen[1][1]                    # the tool results, then the platform's "step limit" note
    assert "step limit" in sent[-1]["text"]
    results = sent[-2]["results"]
    assert [("error" in r["response"]) for r in results] == [False, False, True]
    assert results[2]["response"]["error"]["code"] == "step_limit" and run["steps"] == 2


def test_an_operator_can_stop_a_run(client, services):
    holder = {}

    def stop_then_continue(messages):
        client.post(f"/agent/runs/{holder['id']}/stop", headers=auth("agent"))
        return call("list_projects")

    use(services, FakeLLM([call("list_projects"), stop_then_continue, call("list_projects"), say("never")]))
    holder["id"] = start(client)["id"]
    run = wait_run(services, holder["id"])
    assert run["status"] == "STOPPED" and run["stop_requested"] is True
    assert run["steps"] == 2 and "stopped" in run["error"]


def test_a_model_failure_ends_the_run_with_a_report(client, services):
    use(services, FakeLLM([call("list_projects"), failing()],
                          report={"outcome": "failed", "summary": "model down", "facts": [], "changes": [],
                                  "unverified": [], "next_steps": ["try later"]}))
    run = wait_run(services, start(client)["id"])
    assert run["status"] == "FINISHED" and run["outcome"] == "failed"
    assert "the model failed" in run["error"] and "429" in run["error"]
    job = services.jobs.list(5, kind="agent_run")[0]
    assert job["status"] == "FAILED"


def test_the_report_has_a_fallback_when_the_model_cannot_write_it(client, services):
    use(services, FakeLLM([say("done")], report=LLMError("no json today")))
    run = wait_run(services, start(client)["id"])
    assert run["outcome"] == "failed" and "no json today" in run["report"]["summary"]


def test_tool_calls_after_the_limit_are_dropped(client, services):
    use(services, FakeLLM([call("list_projects"), call("list_projects"), call("create_project", name="late")]))
    run = wait_run(services, start(client, max_steps=2)["id"])
    assert run["steps"] == 2 and client.get("/projects", headers=auth("viewer")).json() == []


def test_token_budget_stops_the_run(client, services):
    services.settings.ai_max_tokens = 250
    use(services, FakeLLM([call("list_projects")] * 10, tokens_per_call=100))
    run = wait_run(services, start(client)["id"])
    assert run["steps"] == 3 and "token budget" in run["error"]


# ------------------------------------------------------------------ approvals
def _propose_run(client, services, board):
    prepare_deploy(services, board)
    llm = use(services, FakeLLM([call("propose_deployment", build_id=1), say("deployed and checked")]))
    run = wait_run(services, start(client, goal="deploy build 1")["id"])
    return llm, run


def test_a_proposal_pauses_the_run_and_flashes_nothing(client, services, board):
    llm, run = _propose_run(client, services, board)
    assert run["status"] == "WAITING_APPROVAL" and run["pending_job_id"]
    job = client.get(f"/jobs/{run['pending_job_id']}", headers=auth("viewer")).json()
    assert job["status"] == "AWAITING_APPROVAL" and job["kind"] == "deploy"
    time.sleep(0.3)
    assert board.flashed == [] and len(llm.seen) == 1
    r = client.post(f"/agent/runs/{run['id']}/resume", headers=auth("agent"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "still_awaiting_approval"
    assert client.post(f"/jobs/{job['id']}/approve", headers=auth("agent"), json={"confirm": "1"}).status_code == 403


def test_after_a_human_approves_the_run_sees_the_deployment(client, services, board):
    llm, run = _propose_run(client, services, board)
    job_id = run["pending_job_id"]
    assert client.post(f"/jobs/{job_id}/approve", headers=auth("operator"), json={"confirm": "1"}).status_code == 200
    assert client.post(f"/agent/runs/{run['id']}/resume", headers=auth("agent")).status_code == 202
    run = wait_run(services, run["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert run["status"] == "FINISHED" and run["outcome"] == "success"
    note = llm.seen[1][1][-1]
    assert note["role"] == "user" and f"job #{job_id}" in note["text"] and "SUCCEEDED" in note["text"]
    assert len(board.flashed) == 1
    approvals = [s for s in services.agents.store.steps(run["id"]) if s["kind"] == "approval"]
    assert [s["name"] for s in approvals] == ["waiting", "decided"] and approvals[1]["ok"] is True


def test_a_rejected_proposal_is_reported_to_the_agent(client, services, board):
    llm, run = _propose_run(client, services, board)
    client.post(f"/jobs/{run['pending_job_id']}/reject", headers=auth("operator"), json={"reason": "not today"})
    client.post(f"/agent/runs/{run['id']}/resume", headers=auth("agent"))
    run = wait_run(services, run["id"], ("FINISHED", "FAILED", "STOPPED"))
    assert "REJECTED" in llm.seen[1][1][-1]["text"] and board.flashed == []


def test_stopping_a_waiting_run(client, services, board):
    _, run = _propose_run(client, services, board)
    r = client.post(f"/agent/runs/{run['id']}/stop", headers=auth("agent"))
    assert r.status_code == 200 and r.json()["status"] == "STOPPED"
    assert client.post(f"/agent/runs/{run['id']}/resume", headers=auth("agent")).status_code == 409
    assert client.post(f"/agent/runs/{run['id']}/stop", headers=auth("agent")).status_code == 409


def test_a_paused_run_survives_a_restart(client, services, board, settings):
    llm, run = _propose_run(client, services, board)
    client.post(f"/jobs/{run['pending_job_id']}/approve", headers=auth("operator"), json={"confirm": "1"})
    services.jobs.wait(run["pending_job_id"], 20)
    # a brand-new service stack on the same database and checkpoint file
    fresh = build_services(settings, lambda: llm)
    fresh.start()
    try:
        out = fresh.agents.resume(run["id"], "test")
        fresh.jobs.wait(out["job_id"], 20)
        assert fresh.agents.get(run["id"])["status"] == "FINISHED"
        assert "SUCCEEDED" in llm.seen[-1][1][-1]["text"]
    finally:
        fresh.stop()


# ------------------------------------------------------------------ the API
def test_agent_api_permissions_and_validation(client, services):
    use(services, FakeLLM([say("ok")]))
    assert client.post("/agent/runs", headers=auth("viewer"), json={"goal": "hello there"}).status_code == 403
    assert client.post("/agent/runs", headers=auth("agent"), json={"goal": "hi"}).status_code == 422
    assert client.post("/agent/runs", headers=auth("agent"), json={"goal": "hello", "max_steps": 99}).status_code == 422
    r = client.post("/agent/runs", headers=auth("agent"), json={"goal": "hello there", "agent": "ghost"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_agent"
    run = wait_run(services, start(client)["id"])
    assert client.get(f"/agent/runs/{run['id']}", headers=auth("viewer")).json()["usage"]["calls"] == 2
    steps = client.get(f"/agent/runs/{run['id']}/steps", headers=auth("viewer")).json()
    assert steps["steps"] and steps["next_after"] == steps["steps"][-1]["id"]
    assert client.get(f"/agent/runs/{run['id']}/messages", headers=auth("agent")).status_code == 403
    msgs = client.get(f"/agent/runs/{run['id']}/messages", headers=auth("operator")).json()
    assert msgs[0]["role"] == "user" and msgs[-1]["text"] == "ok"
    assert client.get("/agent/runs/999", headers=auth("viewer")).status_code == 404
    assert client.get("/agent/runs", headers=auth("viewer")).json()[0]["id"] == run["id"]
    assert {"lab_agent", "platform", "run_report", "supervisor", "verification_agent"} <= {p["name"] for p in client.get("/agent/prompts", headers=auth("viewer")).json()}


def test_runs_refused_when_no_model_is_configured(client, services):
    services.agents._custom_llm = False
    r = client.post("/agent/runs", headers=auth("agent"), json={"goal": "hello there"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ai_not_configured"
    assert client.get("/agent/status", headers=auth("viewer")).json()["configured"] is False


def test_written_sources_are_not_copied_into_the_steps(client, services):
    use(services, FakeLLM([call("create_project", name="demo"),
                           call("write_source", project="demo", filename="a.c", content="int SECRET_SAUCE;"),
                           say("ok")]))
    run = wait_run(services, start(client)["id"])
    text = str(services.agents.store.steps(run["id"]))
    assert "SECRET_SAUCE" not in text and "<17 chars>" in text


def test_orphaned_runs_are_failed_on_start_but_not_when_only_built(settings):
    from app.pipeline.store import PipelineStore

    build_services(settings)
    store, pipes = RunStore(settings.db_path), PipelineStore(settings.db_path)
    run = store.create("lab", "goal", "t", 5)
    store.update(run["id"], status="RUNNING")
    pipe = pipes.create("a requirement", "t", {})
    pipes.update(pipe["id"], status="RUNNING")
    build_services(settings)                       # e.g. importing the app: must not touch running work
    assert store.get(run["id"])["status"] == "RUNNING" and pipes.get(pipe["id"])["status"] == "RUNNING"
    s = build_services(settings)
    s.start()
    s.stop()
    assert store.get(run["id"])["status"] == "FAILED" and pipes.get(pipe["id"])["status"] == "FAILED"


# ------------------------------------------------------------------ pieces
def test_prompts_parse_and_render():
    p = prompts.load("lab_agent")
    assert p.version >= 1 and len(p.sha256) == 64
    assert "PLATFORM-TEXT" in p.render(platform="PLATFORM-TEXT") and "$platform" not in p.render(platform="x")
    plat = prompts.load("platform")
    assert "(25)" in plat.render(max_steps=25) and "$max_steps" not in plat.render(max_steps=25)
    with pytest.raises(prompts.PromptError):
        p.render()
    with pytest.raises(prompts.PromptError):
        prompts.parse("no header")
    with pytest.raises(prompts.PromptError):
        prompts.parse("---\nname: x\n---\ntext")          # no version
    with pytest.raises(prompts.PromptError):
        prompts.load("nope")
    q = prompts.parse("---\r\nname: x\r\nversion: 2\r\n---\r\nhello\r\n")
    assert q.version == 2 and q.text == "hello\n"


def test_gemini_message_conversion_keeps_signatures():
    from google.genai import types

    llm = GeminiLLM("not-a-real-key", "gemini-test")
    sig = base64.b64encode(b"signature").decode()
    contents = llm._contents([
        {"role": "user", "text": "hi"},
        {"role": "model", "text": "", "calls": [{"id": "a", "name": "device_status", "args": {}, "sig": sig}]},
        {"role": "tool", "results": [{"id": "a", "name": "device_status", "response": {"result": {"ok": 1}}}]},
    ])
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].thought_signature == b"signature"
    assert contents[2].parts[0].function_response.response == {"result": {"ok": 1}}
    resp = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[
            types.Part(text="thinking...", thought=True),
            types.Part(text="Let me check."),
            types.Part(function_call=types.FunctionCall(name="device_status", args={}), thought_signature=b"s2"),
        ]), finish_reason=types.FinishReason.STOP)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=5,
                                                                  thoughts_token_count=3, total_token_count=18))
    reply = llm._reply(resp, 2)
    assert reply.text == "Let me check." and reply.calls[0].name == "device_status"
    assert base64.b64decode(reply.calls[0].sig) == b"s2" and reply.usage.total == 18 and reply.attempts == 2
    with pytest.raises(LLMError):
        llm._reply(types.GenerateContentResponse(candidates=[]), 1)


def test_which_errors_are_retried():
    from google.genai import errors

    assert _retryable(errors.ClientError(429, {"error": {"message": "slow down"}}))
    assert _retryable(errors.ServerError(503, {"error": {"message": "busy"}}))
    assert not _retryable(errors.ClientError(400, {"error": {"message": "bad request"}}))
    assert not _retryable(ValueError("x"))


def test_gemini_needs_a_key():
    with pytest.raises(LLMError):
        GeminiLLM("", "gemini-test")


def test_usage_log_totals(settings):
    from app.ai.llm import Usage
    from app.db import migrate

    migrate(settings.db_path)
    log = UsageLog(settings.db_path)
    log.record(run_id=1, purpose="p", model="m", usage=Usage(10, 5, 1, 16), latency_ms=7, attempts=1, ok=True)
    log.record(run_id=1, purpose="p", model="m", usage=Usage(), latency_ms=3, attempts=3, ok=False, error="429")
    log.record(run_id=2, purpose="p", model="m", usage=Usage(1, 1, 0, 2), latency_ms=1, attempts=1, ok=True)
    t = log.totals(1)
    assert (t["calls"], t["failed"], t["total"], t["latency_ms"]) == (2, 1, 16, 10)
    assert log.totals()["calls"] == 3 and len(log.calls(1)) == 2
