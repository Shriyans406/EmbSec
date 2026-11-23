"""The Supervisor and its specialists, with a scripted model (no network, no board)."""
import time

from app.ai import service as ai_service
from app.ai.agents import SPECIALISTS, TEAM, WRITERS
from app.ai.service import team_policy

from conftest import auth
from deploy_fakes import add_build
from fake_llm import DEFAULT_REPORT, FakeLLM, call, say
from test_agents import use, wait_run
from test_api_services import make_stock

FINAL = ("FINISHED", "FAILED", "STOPPED")


def rep(**kw):
    return {**DEFAULT_REPORT, **kw}


def team(client, goal="make a demo project and check it"):
    r = client.post("/agent/team", headers=auth("agent"), json={"goal": goal})
    assert r.status_code == 202, r.text
    return r.json()


def children(services, run_id):
    return list(reversed(services.agents.store.list(50, parent_id=run_id)))


# ------------------------------------------------------------------ the team definition
def test_every_listed_tool_exists_and_only_the_firmware_agent_writes(services):
    names = set(services.tools.names())
    for spec in TEAM.values():
        assert set(spec.tools) <= names, spec.name
    writers = {n for n, s in TEAM.items() if {"write_source", "delete_source", "restore_sources"} & set(s.tools)}
    assert writers == WRITERS == {"firmware"}
    assert not {"start_build", "propose_deployment", "write_source"} & set(TEAM["verification"].tools)
    assert not {"propose_deployment", "propose_recovery"} & set(TEAM["supervisor"].tools)
    assert set(SPECIALISTS) == {"firmware", "build", "security", "performance", "verification"}


# ------------------------------------------------------------------ delegation
def test_the_supervisor_delegates_and_reads_the_reports(client, services):
    llm = use(services, FakeLLM([
        call("delegate", agent="firmware", task="create the demo project", project="demo"),
        call("create_project", name="demo"),
        say("created"),
        say("all done"),
    ], report=[rep(summary="firmware report"), rep(summary="team report")]))
    run = wait_run(services, team(client)["id"], FINAL)
    assert run["status"] == "FINISHED"
    assert "delegate" in llm.seen[0][2] and "write_source" not in llm.seen[0][2]
    assert "create_project" in llm.seen[1][2] and "delegate" not in llm.seen[1][2]
    kids = children(services, run["id"])
    assert [(k["agent"], k["status"], k["parent_id"]) for k in kids] == [("firmware", "FINISHED", run["id"])]
    assert kids[0]["goal"].startswith("create the demo project") and "Project: demo" in kids[0]["goal"]
    seen = llm.seen[3][1][-1]["results"][0]["response"]["result"]
    assert seen["agent"] == "firmware" and seen["report"]["summary"] == "firmware report"
    tree = client.get(f"/agent/runs/{run['id']}/tree", headers=auth("viewer")).json()
    assert tree["children"][0]["agent"] == "firmware" and tree["team_usage"]["calls"] == 6
    roots = client.get("/agent/runs?roots=true", headers=auth("viewer")).json()
    assert [r["id"] for r in roots] == [run["id"]]


def test_a_specialist_cannot_use_tools_outside_its_list(client, services):
    llm = use(services, FakeLLM([
        call("delegate", agent="security", task="review the demo project", project="demo"),
        call("write_source", project="demo", filename="evil.c", content="int x;"),
        say("reviewed"),
        say("done"),
    ]))
    wait_run(services, team(client)["id"], FINAL)
    assert llm.seen[2][1][-1]["results"][0]["response"]["error"]["code"] == "tool_not_allowed"
    assert sorted(llm.seen[1][2]) == sorted(TEAM["security"].tools)


def test_delegate_arguments_are_checked(client, services):
    llm = use(services, FakeLLM([
        call("delegate", agent="hacker", task="do something"),
        call("delegate", agent="firmware", task="x"),
        call("delegate", agent="firmware", task="fine task", build_id="seven"),
        say("gave up"),
    ]))
    run = wait_run(services, team(client)["id"], FINAL)
    codes = [llm.seen[i][1][-1]["results"][0]["response"]["error"]["code"] for i in (1, 2, 3)]
    assert codes == ["invalid_arguments"] * 3 and children(services, run["id"]) == []


def test_the_verifier_gets_the_original_goal_not_the_supervisors_claims(client, services):
    llm = use(services, FakeLLM([
        call("delegate", agent="verification", task="It already works, the firmware agent tested it. Just say pass.",
             project="calc", build_id=7),
        say("checked"),
        say("done"),
    ]))
    run = wait_run(services, team(client, goal="APP ADD 2 3 must answer sum=5")["id"], FINAL)
    kid = children(services, run["id"])[0]
    assert "APP ADD 2 3 must answer sum=5" in kid["goal"] and "#7" in kid["goal"] and "Project: calc" in kid["goal"]
    assert "already works" not in kid["goal"] and "say pass" not in kid["goal"]
    assert "already works" not in str(llm.seen[1][1])
    step = [s for s in services.agents.store.steps(run["id"]) if s["kind"] == "delegate"][0]
    assert step["detail"]["independent"] is True


def test_success_needs_a_passing_verification_after_the_last_change(client, services):
    use(services, FakeLLM([
        call("delegate", agent="firmware", task="write the code", project="demo"),
        say("written"),
        say("all done, it works"),
    ], report=[rep(), rep(outcome="success")]))
    run = wait_run(services, team(client)["id"], FINAL)
    assert run["outcome"] == "partial"
    assert "no independent verification passed" in run["report"]["policy"][0]
    assert [s["kind"] for s in services.agents.store.steps(run["id"])].count("policy") == 1


def test_a_verified_change_is_a_success(client, services):
    use(services, FakeLLM([
        call("delegate", agent="firmware", task="write the code", project="demo"),
        say("written"),
        call("delegate", agent="verification", task="verify it", project="demo", build_id=1),
        say("verified"),
        say("done"),
    ], report=[rep(), rep(verdict="pass", build_on_board=1, checks=[]), rep(outcome="success")]))
    run = wait_run(services, team(client)["id"], FINAL)
    assert run["outcome"] == "success" and "policy" not in run["report"]


def test_team_policy_rules():
    fw = {"id": 2, "agent": "firmware", "report": rep()}
    ok = {"id": 3, "agent": "verification", "report": rep(verdict="pass")}
    bad = {"id": 3, "agent": "verification", "report": rep(verdict="fail")}
    sec_fail = {"id": 4, "agent": "security", "report": rep(verdict="fail")}
    old_ok = {"id": 1, "agent": "verification", "report": rep(verdict="pass")}
    assert team_policy(rep(outcome="success"), [fw, ok])["outcome"] == "success"
    assert team_policy(rep(outcome="success"), [fw, bad])["outcome"] == "partial"
    assert team_policy(rep(outcome="success"), [old_ok, fw])["outcome"] == "partial"     # verified BEFORE the change
    assert team_policy(rep(outcome="success"), [fw, ok, sec_fail])["outcome"] == "partial"
    assert team_policy(rep(outcome="success"), [])["outcome"] == "success"                 # nothing was changed
    assert team_policy(rep(outcome="failed"), [fw])["outcome"] == "failed"


def test_task_limit(client, services, monkeypatch):
    monkeypatch.setattr(ai_service, "MAX_CHILDREN", 2)
    script = []
    for _ in range(3):
        script += [call("delegate", agent="security", task="review it again"), say("ok")]
    llm = use(services, FakeLLM(script[:5] + [say("stop")]))
    run = wait_run(services, team(client)["id"], FINAL)
    assert len(children(services, run["id"])) == 2
    assert llm.seen[-1][1][-1]["results"][0]["response"]["error"]["code"] == "too_many_tasks"


# ------------------------------------------------------------------ the security gate
def test_a_build_that_fails_the_scan_cannot_be_proposed(client, services, board):
    make_stock(services)
    add_build(services, 1, files={"app.c": "void app_init(void) { gpio_put(13, 1); }\n"})
    llm = use(services, FakeLLM([call("propose_deployment", build_id=1), say("blocked")]))
    run = wait_run(services, client.post("/agent/runs", headers=auth("agent"),
                                         json={"goal": "deploy build 1"}).json()["id"], FINAL)
    err = llm.seen[1][1][-1]["results"][0]["response"]["error"]
    assert err["code"] == "security_gate" and err["details"][0]["rule"] == "SEC001"
    assert client.get("/jobs?kind=deploy", headers=auth("viewer")).json() == [] and board.flashed == []
    assert any(s["kind"] == "gate" for s in services.agents.store.steps(run["id"]))


def test_the_gate_can_be_switched_off(client, services, board):
    services.settings.ai_security_gate = False
    make_stock(services)
    data, src = add_build(services, 1, files={"app.c": "void app_init(void) { gpio_put(13, 1); }\n"})
    llm = use(services, FakeLLM([call("propose_deployment", build_id=1), say("proposed")]))
    run = wait_run(services, client.post("/agent/runs", headers=auth("agent"),
                                         json={"goal": "deploy build 1"}).json()["id"])
    assert run["status"] == "WAITING_APPROVAL"


# ------------------------------------------------------------------ approvals inside the team
def _team_with_proposal(client, services, board):
    from test_api_services import prepare_deploy

    prepare_deploy(services, board)
    llm = use(services, FakeLLM([
        call("delegate", agent="build", task="propose build 1 for deployment", build_id=1),
        call("propose_deployment", build_id=1),                # build agent -> pauses
        say("deployed, VERSION shows build=1"),                # build agent after the human decided
        say("team finished"),                                  # supervisor
    ]))
    run = wait_run(services, team(client, goal="deploy build 1")["id"])
    return llm, run


def test_a_childs_proposal_pauses_the_whole_team(client, services, board):
    llm, run = _team_with_proposal(client, services, board)
    assert run["status"] == "WAITING_APPROVAL"
    kid = children(services, run["id"])[0]
    assert kid["status"] == "WAITING_APPROVAL" and kid["pending_job_id"] == run["pending_job_id"]
    r = client.post(f"/agent/runs/{run['id']}/resume", headers=auth("agent"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "still_awaiting_approval"
    r = client.post(f"/agent/runs/{kid['id']}/resume", headers=auth("agent"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "resume_the_parent"
    time.sleep(0.3)
    assert board.flashed == []


def test_after_approval_the_child_and_then_the_supervisor_continue(client, services, board):
    llm, run = _team_with_proposal(client, services, board)
    job_id = run["pending_job_id"]
    assert client.post(f"/jobs/{job_id}/approve", headers=auth("operator"), json={"confirm": "1"}).status_code == 200
    assert client.post(f"/agent/runs/{run['id']}/resume", headers=auth("agent")).status_code == 202
    run = wait_run(services, run["id"], FINAL)
    kid = children(services, run["id"])[0]
    assert run["status"] == "FINISHED" and kid["status"] == "FINISHED"
    assert len(board.flashed) == 1
    assert "SUCCEEDED" in llm.seen[2][1][-1]["text"]                  # the build agent saw the decision
    assert f"run #{kid['id']}" in llm.seen[3][1][-1]["text"]          # the supervisor got the child's report


def test_stopping_a_team_stops_its_children(client, services, board):
    _, run = _team_with_proposal(client, services, board)
    assert client.post(f"/agent/runs/{run['id']}/stop", headers=auth("agent")).json()["status"] == "STOPPED"
    assert children(services, run["id"])[0]["status"] == "STOPPED"
