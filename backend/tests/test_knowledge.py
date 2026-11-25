"""The knowledge base: storage and search, collection from records, histories, and memory in the agents' context."""
import json
import time

import pytest

from app.db import connect, now_iso
from app.fuzz.oracle import Observation, Verdict
from app.knowledge.store import KnowledgeStore, _clean_query

from conftest import auth
from deploy_fakes import add_build
from fake_llm import FakeLLM, say
from test_agents import use, wait_run
from test_api_services import approve, prepare_deploy, wait_done


def plant_parser_bug(services, project="serial1"):
    """What a first serial-parser project left behind: a fuzz finding and the remediation that fixed it."""
    store = services.fuzz.store
    camp = store.create(project, 7, ["oversize"], {}, "test")
    camp = {**camp, "build_id": 3, "src_hash": "abcd1234"}
    obs = Observation(text="APP SET " + "9" * 40, family="oversize", reply=None, error="link_lost",
                      note="huge number argument")
    v = Verdict("crash", "high", "no reply and the board restarted (reset reason SOFTWARE_REBOOT)",
                "CWE-787/CWE-120 out-of-bounds write or unhandled fault suspected")
    fid, _ = store.record(camp, "crash:APP SET", v, obs, project)
    store.update_finding(fid, status="fixed", minimized="APP SET " + "9" * 25)
    rem = services.remediations.store.create(fid, project, "test", {})
    services.remediations.store.update(
        rem["id"], status="FINISHED", outcome="fixed", before_build=3, after_build=4,
        root_cause="The serial command parser copied the argument into char buf[16] with sscanf %s (no width): "
                   "a long number overflowed the stack buffer",
        diff_text="-    sscanf(args, \"%s %s\", cmd, buf);\n+    sscanf(args, \"%7s %15s\", cmd, buf);\n")
    services.remediations.store.set_state(rem["id"], evidence={"finding": {"kind": "crash", "minimized": "APP SET "
                                                                          + "9" * 25, "cwe": v.cwe}},
                                          retest=[{"input": "APP SET " + "9" * 25, "hits": 0, "attempts": 5}])
    return fid, rem["id"]


# ------------------------------------------------------------------ the store
def test_store_search_and_upsert(tmp_path):
    from app.db import migrate

    migrate(tmp_path / "k.db")
    k = KnowledgeStore(tmp_path / "k.db")
    a = k.upsert("bug", "parser crash", "sscanf without width overflowed buf", project="p1", source_type="x",
                 source_id=1, tags=["crash", "Parser"])
    b = k.upsert("bug", "parser crash (again)", "the parsing overflow is still there", project="p1", source_type="x",
                 source_id=1)
    assert a["id"] == b["id"] and k.stats()["total"] == 1                 # the same record: updated, not duplicated
    k.upsert("note", "LED", "GPIO 4 drives the LED", project="p2")
    hits = k.search("parsers overflowing")                                # stemmed: parser / overflow
    assert [h["id"] for h in hits] == [a["id"]] and "[overflow]" in hits[0]["snippet"]
    assert k.search('" OR 1=1; DROP TABLE knowledge --') == [] and k.search("NEAR(a b)") == []
    assert _clean_query("the firmware must parse the APP command quickly") == '"parse" OR "quickly"'
    with pytest.raises(Exception):
        k.upsert("gossip", "x", "y")
    assert k.related("parser overflow", project="p1")[0]["id"] == a["id"]
    assert k.delete(a["id"]) == {"deleted": a["id"]} and k.search("parser") == []


# ------------------------------------------------------------------ collection
def test_records_become_knowledge_once(client, services):
    fid, rid = plant_parser_bug(services)
    add_build(services, 1, project="serial1", status="FAILED",
              diagnostics=[{"severity": "error", "message": "'buf' undeclared", "file": "app/app.c", "line": 7,
                            "column": 3, "flag": None, "source": "app", "category": "compile", "context": []}])
    first = services.knowledge.collect()
    assert first["bugs"] == 1 and first["fixes"] == 1 and first["build_failures"] == 1
    total = services.knowledge.store.stats()["total"]
    services.knowledge.collect()
    assert services.knowledge.store.stats()["total"] == total                 # collecting again adds nothing
    fix = services.knowledge.store.list(kind="fix")[0]
    assert fix["source_type"] == "remediation" and fix["source_id"] == rid and fix["build_id"] == 4
    assert "sscanf %s (no width)" in fix["body"] and "%15s" in fix["body"]
    bug = services.knowledge.store.list(kind="bug")[0]
    assert bug["source_id"] == fid and bug["build_id"] == 3 and bug["src_hash"] == "abcd1234" and "fixed" in bug["body"]


def test_rejected_fixes_and_optimisations_are_decisions(client, services):
    fid, _ = plant_parser_bug(services)
    rem = services.remediations.store.create(fid, "serial1", "t", {})
    services.remediations.store.update(rem["id"], status="FINISHED", outcome="rejected_rolled_back", before_build=3,
                                       root_cause="unbounded copy")
    services.remediations.store.set_state(rem["id"], rejection=["calc-checks/mul: 'OK 6' -> 'OK 5'"])
    o = services.optimizations.store.create("serial1", "speed", "mul", 10, "t", {})
    services.optimizations.store.update(o["id"], status="FINISHED", outcome="accepted", base_build=3, variant_build=5)
    services.optimizations.store.set_state(o["id"], proposal="use a * b instead of a loop", target="mul", comparison={
        "commands": {"mul": {"device_us": {"before": 40, "after": 12, "change_pct": -70}}}},
        tried=[{"variant": 1, "proposal": "lookup table", "why": "flash grows by 30 %"}])
    services.knowledge.collect()
    dec = services.knowledge.store.list(kind="decision")[0]
    assert "rejected_rolled_back" in dec["title"] and "'OK 5'" in dec["body"]
    opt = services.knowledge.store.list(kind="optimisation")[0]
    assert "40 -> 12 (-70 %)" in opt["body"] and "lookup table - rejected: flash grows by 30 %" in opt["body"]
    assert opt["build_id"] == 5


# ------------------------------------------------------------------ memory: the completion test of the phase
def test_a_second_serial_parser_task_is_told_about_the_first_ones_bug(client, services):
    plant_parser_bug(services, "serial1")
    llm = use(services, FakeLLM([say("noted")]))
    r = client.post("/agent/runs", headers=auth("agent"),
                    json={"goal": "Write the serial command parser for the thermostat: APP SET <temperature> and "
                                  "APP GET; reject numbers that are too long.", "project": "thermo"})
    run = wait_run(services, r.json()["id"])
    first = llm.seen[0][1][0]["text"]
    assert "Lessons from earlier work in this lab" in first
    assert "serial1" in first and "sscanf %s (no width)" in first and "crash on APP SET" in first
    step = next(s for s in services.agents.store.steps(run["id"]) if s["kind"] == "memory")
    assert step["detail"]["project"] == "thermo" and len(step["detail"]["entries"]) >= 2
    assert run["goal"].startswith("Write the serial command parser")        # the goal itself stays clean


def test_memory_reaches_inline_agents_and_can_be_switched_off(client, services):
    plant_parser_bug(services)
    llm = use(services, FakeLLM([say("ok"), say("ok")]))
    services.agents.run_inline("firmware", "Fix the serial parser: long numbers crash it", "test", project="serial2")
    assert "Lessons from earlier work" in llm.seen[0][1][0]["text"]
    services.settings.ai_memory = False
    services.agents.run_inline("firmware", "Fix the serial parser: long numbers crash it", "test", project="serial2")
    assert "Lessons from earlier work" not in llm.seen[1][1][0]["text"]


def test_nothing_relevant_means_no_lessons(client, services):
    plant_parser_bug(services)
    llm = use(services, FakeLLM([say("ok")]))
    services.agents.run_inline("firmware", "Blink the LED at 2 Hz", "test", project="blinky")
    assert "Lessons from earlier work" not in llm.seen[0][1][0]["text"]


# ------------------------------------------------------------------ the API, histories, decisions
def test_knowledge_api(client, services):
    plant_parser_bug(services)
    hits = client.get("/knowledge/search?q=stack+buffer+overflow", headers=auth("viewer")).json()
    assert hits and hits[0]["kind"] == "fix"
    assert client.get("/knowledge/search?q=board+restarted+bounds&kind=bug", headers=auth("viewer")).json()[0]["kind"] == "bug"
    rel = client.get("/knowledge/related?q=serial+parser+long+numbers&project=x", headers=auth("viewer")).json()
    assert rel["entries"] and "Lessons" in rel["context"]
    stats = client.get("/knowledge/stats", headers=auth("viewer")).json()
    assert stats["by_kind"]["bug"] == 1 and "note" in stats["kinds"]
    d = client.post("/knowledge/decisions", headers=auth("agent"), json={
        "title": "always give sscanf a width", "context": "a crash in serial1", "decision": "%Ns for every %s",
        "consequences": "longer format strings", "project": "serial1"})
    assert d.status_code == 201 and d.json()["kind"] == "decision" and d.json()["importance"] == 3
    assert client.post("/knowledge/notes", headers=auth("viewer"), json={"title": "x y z", "body": "a b c"}).status_code == 403
    kid = d.json()["id"]
    assert client.get(f"/knowledge/{kid}", headers=auth("viewer")).json()["source_type"] == "adr"
    assert client.delete(f"/knowledge/{kid}", headers=auth("agent")).status_code == 403
    assert client.delete(f"/knowledge/{kid}", headers=auth("operator")).status_code == 200
    assert client.get("/knowledge/search?q=x", headers=auth("viewer")).status_code == 422          # too short


def test_timeline_and_board_history(client, services, board):
    prepare_deploy(services, board)
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": 1}).json()["id"]
    approve(client, jid, "1")
    wait_done(client, jid)
    client.post("/knowledge/notes", headers=auth("agent"), json={"title": "demo runs at 125 MHz",
                                                                 "body": "measured", "project": "demo"})
    tl = client.get("/projects/demo/timeline", headers=auth("viewer")).json()
    types = [e["type"] for e in tl]
    assert {"build", "deployment", "note"} <= set(types)
    dep = next(e for e in tl if e["type"] == "deployment")
    assert "approved by api:operator" in dep["summary"]
    h = client.get("/boards/DE61C48787331A35/history", headers=auth("viewer")).json()
    assert h["current"]["build_id"] == 1 and h["current"]["approved_by"].startswith("api:operator")
    assert client.get("/boards/NOPE/history", headers=auth("viewer")).status_code == 404


def test_knowledge_tools(client, services):
    plant_parser_bug(services)
    call = lambda name, args, role="agent": client.post(f"/tools/{name}/call", headers=auth(role),
                                                       json={"arguments": args})
    found = call("search_knowledge", {"query": "serial parser overflow"}, "viewer").json()["result"]
    assert found and call("get_knowledge", {"entry_id": found[0]["id"]}, "viewer").json()["result"]["id"] == found[0]["id"]
    r = call("record_decision", {"title": "widths on sscanf", "context": "crash", "decision": "always %Ns"})
    assert r.json()["result"]["kind"] == "decision"
    assert call("record_decision", {"title": "x y", "context": "c c", "decision": "d d"}, "viewer").status_code == 403
    assert call("project_timeline", {"project": "serial1"}, "viewer").status_code == 200
