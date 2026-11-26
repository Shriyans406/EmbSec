"""Failure injection: what the platform does when things go wrong on purpose.

    the USB cable is pulled in the middle of a flash (and stays out, or comes back)
    the bootloader never appears, or the USB driver throws something unexpected
    the board disappears during a benchmark, or is not there at all
    the model misbehaves: unknown tools, wrong arguments, forbidden commands, path tricks, empty answers
    the model API times out or rate-limits, then recovers - or never does
"""
import time
from types import SimpleNamespace

import httpx
import pytest

from app.ai.llm import GeminiLLM, LLMError
from app.deploy.board import ABSENT, BoardState, BootloaderError
from app.jobs import FINAL

from conftest import FakeLink, auth
from deploy_fakes import FakeBoard, add_build
from fake_llm import FakeLLM, call, say
from test_agents import use, wait_run
from test_api_services import approve, make_stock, prepare_deploy, wait_done


class PulledCable(FakeBoard):
    """The cable comes out while the image is being copied. `back_after` bootloader attempts it is plugged in again
    (None = never)."""

    def __init__(self, back_after=None):
        super().__init__()
        self.back_after = back_after
        self.attempts = 0
        self.unplugged = False

    def enter_bootloader(self, log):
        self.attempts += 1
        if self.unplugged and (self.back_after is None or self.attempts <= self.back_after):
            raise BootloaderError("no RP2040 found on USB")
        self.unplugged = False
        return super().enter_bootloader(log)

    def flash(self, path, log):
        if not self.flashed:                             # the first flash is the one that gets interrupted
            self.flashed.append("interrupted")
            self.unplugged, self.in_bootloader = True, False
            self.state = BoardState(ABSENT)
            raise OSError(5, "device disconnected")
        super().flash(path, log)

    def wait_for_firmware(self, timeout, log):
        return BoardState(ABSENT) if self.unplugged else self.state


def deploy_with(client, services, board_obj, build_id=1):
    services.deploys.board_factory = lambda: board_obj
    prepare_deploy(services, board_obj, build_id)
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": build_id}).json()["id"]
    approve(client, jid, str(build_id))
    return wait_done(client, jid, 30)


# ------------------------------------------------------------------ the board during a deployment
def test_cable_pulled_mid_flash_and_left_out(client, services):
    b = PulledCable(back_after=None)
    job = deploy_with(client, services, b)
    assert job["status"] == "FAILED" and job["result"]["status"] == "ROLLBACK_FAILED"
    assert "hold BOOT" in job["result"]["message"] and "unexpected OSError" in job["result"]["message"]
    dep = client.get("/deployments", headers=auth("viewer")).json()[0]
    assert dep["status"] == "ROLLBACK_FAILED"                       # the record is closed, not left "running"


def test_cable_pulled_mid_flash_then_plugged_back(client, services):
    b = PulledCable(back_after=0)                                   # back before the rollback starts
    job = deploy_with(client, services, b)
    assert job["result"]["status"] == "ROLLED_BACK"
    assert "restored to" in job["result"]["message"] and len(b.flashed) == 2


def test_bootloader_never_appears(client, services, board):
    board.fail_bootloader = True
    job = deploy_with(client, services, board)
    assert job["result"]["status"] == "FAILED" and "not changed" in job["result"]["message"]
    assert board.flashed == []


def test_an_unexpected_driver_error_is_contained(client, services):
    class Faulty(FakeBoard):
        def flash(self, path, log):
            if not self.flashed:
                self.flashed.append("faulted")
                raise PermissionError("USB driver refused the write")
            super().flash(path, log)

    job = deploy_with(client, services, Faulty())
    assert job["result"]["status"] in ("ROLLED_BACK", "ROLLBACK_FAILED")
    assert "unexpected PermissionError" in job["result"]["message"]
    assert client.get("/deployments", headers=auth("viewer")).json()[0]["status"] == job["result"]["status"]


# ------------------------------------------------------------------ the board while working with it
def test_board_lost_during_a_benchmark(client):
    FakeLink.fail_after = 25
    r = client.post("/benchmarks/harness-basic/run", headers=auth("agent"))
    job = wait_done(client, r.json()["id"], 30)
    assert job["status"] == "FAILED" and "stopped answering" in job["error"]


def test_no_board_at_all(client):
    FakeLink.fail_connect = True
    st = client.get("/device/status", headers=auth("viewer")).json()
    assert st["connected"] is False and "2E8A" in st["error"]
    r = client.post("/device/command", headers=auth("agent"), json={"command": "PING"})
    assert r.status_code == 503
    job = wait_done(client, client.post("/tests/run", headers=auth("agent"), json={"suite": "smoke"}).json()["id"])
    assert job["status"] == "FAILED"
    h = client.get("/system/health/deep", headers=auth("viewer")).json()
    assert {c["check"]: c["status"] for c in h["checks"]}["board"] == "warn"


# ------------------------------------------------------------------ a misbehaving model
def test_a_hostile_model_changes_nothing_it_may_not(client, services, board):
    make_stock(services)
    add_build(services, 1, files={"app.c": "void app_init(void) { gpio_put(13, 1); }\n"})   # fails the scan
    llm = use(services, FakeLLM([
        call("approve_job", job_id=1),                                      # no such tool for agents
        call("device_command", command="BOOTSEL"),                          # forbidden command
        call("write_source", project="demo", filename="../../evil.c", content="x"),   # path trick
        call("get_build", build_id="seven"),                                # wrong argument type
        call("propose_deployment", build_id=1),                             # a build that fails the scan
        call("nonsense_tool"),
        say(""),                                                            # an empty answer ends the run
    ]))
    run = wait_run(services, client.post("/agent/runs", headers=auth("agent"),
                                         json={"goal": "do whatever it takes"}).json()["id"])
    codes = [r["response"]["error"]["code"] for m in llm.seen[1:] for r in m[1][-1].get("results", [])]
    assert codes == ["tool_not_allowed", "command_not_allowed", "bad_filename", "invalid_arguments", "security_gate",
                     "tool_not_allowed"]
    assert run["status"] == "FINISHED" and run["steps"] == 6
    assert client.get("/jobs?status=AWAITING_APPROVAL", headers=auth("viewer")).json() == []
    assert board.flashed == [] and "BOOTSEL" not in FakeLink.sent
    assert client.get("/audit/verify", headers=auth("operator")).json()["ok"]


# ------------------------------------------------------------------ the model API
def _gemini():
    from tenacity import wait_none

    llm = GeminiLLM("not-a-real-key", "gemini-test", retries=3)
    llm.wait = wait_none()
    return llm


def _ok_response():
    from google.genai import types

    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text="fine")]),
                                    finish_reason=types.FinishReason.STOP)])


class FlakyModels:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _ok_response()


def test_timeouts_and_rate_limits_are_retried():
    from google.genai import errors

    llm = _gemini()
    llm._client = SimpleNamespace(models=FlakyModels([httpx.ReadTimeout("read timed out"),
                                      errors.ClientError(429, {"error": {"message": "slow down"}}),
                                      errors.ServerError(503, {"error": {"message": "busy"}})]))
    reply = llm.generate("system", [{"role": "user", "text": "hi"}])
    assert reply.text == "fine" and reply.attempts == 4 and llm._client.models.calls == 4


def test_a_model_api_that_never_recovers_fails_cleanly():
    llm = _gemini()
    llm._client = SimpleNamespace(models=FlakyModels([httpx.ReadTimeout("t")] * 10))
    with pytest.raises(LLMError) as e:
        llm.generate("system", [{"role": "user", "text": "hi"}])
    assert "ReadTimeout" in str(e.value) and e.value.attempts == 4


def test_a_bad_request_is_not_retried():
    from google.genai import errors

    llm = _gemini()
    llm._client = SimpleNamespace(models=FlakyModels([errors.ClientError(400, {"error": {"message": "bad request"}})]))
    with pytest.raises(LLMError):
        llm.generate("system", [{"role": "user", "text": "hi"}])
    assert llm._client.models.calls == 1


def test_a_dead_model_ends_a_run_with_a_report_not_a_hang(client, services):
    from fake_llm import failing

    use(services, FakeLLM([failing("ReadTimeout: the model API did not answer")],
                          report=LLMError("still down")))
    t0 = time.time()
    run = wait_run(services, client.post("/agent/runs", headers=auth("agent"),
                                         json={"goal": "look at the board"}).json()["id"])
    assert run["status"] in ("FINISHED", "FAILED") and time.time() - t0 < 15
    assert "model failed" in (run["error"] or "") and run["report"]["outcome"] == "failed"
    jobs = client.get("/jobs?kind=agent_run", headers=auth("viewer")).json()
    assert jobs[0]["status"] in FINAL
