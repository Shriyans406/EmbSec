"""The fuzzer against the simulated board, with bugs planted on purpose."""
import json
import random

import pytest

from app.fuzz.generators import FAMILIES, Generator, command_shapes, corpus_from, is_safe
from app.fuzz.oracle import Observation, judge, signature
from app.pipeline.store import PipelineStore
from app.services.device import DeviceManager

from conftest import auth
from sim_board import SimFirmware, SimLink
from test_api_services import wait_done

SPEC = {"project": "calc", "commands": [
    {"name": n, "usage": f"APP {n} <a> <b>", "examples": [{"request": f"APP {n} 6 3"}]} for n in ("ADD", "MUL", "DIV")]}


@pytest.fixture()
def fw(services):
    """Put a simulated board behind every service that talks to the board."""
    firmware = SimFirmware()
    services.device = DeviceManager(link_factory=lambda: SimLink(firmware))
    for svc in (services.tests, services.analysis, services.fuzz, services.deploys):
        svc.device = services.device
    services.fuzz.recover_s = 0.3
    store = PipelineStore(services.settings.db_path)
    p = store.create("a calculator", "t", {})
    store.update(p["id"], project="calc", spec_json=json.dumps(SPEC))
    return firmware


def campaign(client, role="agent", **body):
    r = client.post("/fuzz/campaigns", headers=auth(role), json={"project": "calc", "seed": 5, **body})
    assert r.status_code == 202, r.text
    job = wait_done(client, r.json()["job_id"], 60)
    return job, client.get(f"/fuzz/campaigns/{r.json()['id']}", headers=auth("viewer")).json()


def kinds(camp):
    return {f["kind"] for f in camp["finding_list"]}


# ------------------------------------------------------------------ generators and the safety filter
def test_generation_is_reproducible_and_covers_every_family():
    corpus, shapes = corpus_from(SPEC), command_shapes(SPEC)
    a = Generator(42, corpus, shapes).generate(FAMILIES, 300)
    b = Generator(42, corpus, shapes).generate(FAMILIES, 300)
    assert [x.text for x in a] == [x.text for x in b] and len(a) <= 300
    assert {x.family for x in a} == set(FAMILIES)
    assert [x.text for x in Generator(43, corpus, shapes).generate(FAMILIES, 300)] != [x.text for x in a]
    assert any(x.text == "APP DIV 1 0" for x in a)                      # a boundary value in the 2nd argument
    assert all(x.partial for x in a if x.family == "timeout")
    small = Generator(1, corpus, shapes).generate(FAMILIES, 16)
    assert len(small) == 16 and len({x.family for x in small}) == len(FAMILIES)


def test_nothing_dangerous_is_ever_generated():
    gen = Generator(7, corpus_from(SPEC) + ["REBOOT", "BOOTSEL", "HANG"], command_shapes(SPEC))
    for _ in range(3000):
        text = gen.mutate(random.Random(_).choice(["PING", "ECHO hello", "BLINK 200", "APP ADD 1 2"]))
        if not is_safe(text):
            continue
        assert "BOOTSEL" not in text.upper() and "REBOOT" not in text.upper()
    for bad in ("BOOTSEL", "bootsel", "#7 BOOTSEL", "  HANG", "REBOOT*1234", "REB OOT", "PING\nBOOTSEL", "HANG 1"):
        assert not is_safe(bad), bad
    assert all(is_safe(x) for x in ("PING", "APP DIV 1 0", "#5 PING*1234", "ECHO " + "A" * 300))
    assert all(is_safe(x.text) for x in gen.generate(FAMILIES, 500))


def test_oracle_rules():
    O = Observation
    assert judge(O("PING", "x", reply="OK PONG")) == []
    assert [v.kind for v in judge(O("APP DIV 1 0", "b", error="timeout", alive_after=True,
                                    reset_reason="WATCHDOG_TIMEOUT"))] == ["hang"]
    assert [v.kind for v in judge(O("X", "b", error="timeout", alive_after=False))] == ["dead"]
    assert [v.kind for v in judge(O("X", "b", error="timeout", alive_after=True))] == ["dropped"]
    assert [v.kind for v in judge(O("PING", "b", reply="OK PONG", uptime_before=900, uptime_after=5))] == ["crash"]
    assert [v.kind for v in judge(O("#5 PING", "m", reply="OK PONG"))] == ["id_mismatch"]
    assert judge(O("#5 PING", "m", reply="#5 OK PONG")) == []
    assert [v.kind for v in judge(O("X", "m", reply="hello"))] == ["garbled"]
    assert [v.kind for v in judge(O("X", "m", reply="ERR 9 WHAT"))] == ["bad_error"]
    assert [v.kind for v in judge(O("PING", "m", reply="OK PONG", latency_ms=900))] == ["slow"]
    assert [v.kind for v in judge(O("PIN", "timeout", partial=True, alive_after=True))] == ["no_timeout"]
    assert [v.kind for v in judge(O("APP ADD 1e3 1", "overflow", reply="OK sum=1001"))] == ["accepted"]
    assert judge(O("A" * 200, "oversize", reply="ERR 5 TOO_LONG too long")) == []
    assert signature("#2 APP div 1 0", "hang") == "hang:APP DIV"


# ------------------------------------------------------------------ campaigns
def test_a_clean_board_gives_no_serious_findings(client, fw):
    job, camp = campaign(client, max_inputs=150)
    assert job["status"] == "SUCCEEDED" and camp["status"] == "FINISHED" and camp["inputs_sent"] == 150
    assert camp["resets"] == 0 and camp["build_id"] == 1
    assert not [f for f in camp["finding_list"] if f["severity"] in ("critical", "high", "medium")]
    assert not any(line.split(" ")[0].upper().lstrip("#0123456789 ") in ("BOOTSEL", "HANG", "REBOOT")
                   for line in fw.log)


def test_planted_hang_and_crash_are_found(client, fw):
    fw.bugs |= {"div_zero_hang", "long_arg_crash"}
    job, camp = campaign(client, max_inputs=400, max_resets=3)
    found = {f["kind"]: f for f in camp["finding_list"]}
    assert {"hang", "crash"} <= set(found)
    assert found["hang"]["input_text"].startswith("APP DIV") and found["hang"]["severity"] == "high"
    assert "CWE-835" in found["hang"]["cwe"] and found["hang"]["build_id"] == 1
    assert camp["resets"] >= 2 and camp["summary"]["by_kind"]["hang"] >= 1


def test_the_reset_budget_stops_a_campaign(client, fw):
    fw.bugs |= {"div_zero_hang", "long_arg_crash"}
    job, camp = campaign(client, max_inputs=400, max_resets=1)
    assert camp["stop_reason"] == "reset budget of 1 used up" and camp["inputs_sent"] < camp["inputs_planned"]


def test_a_board_that_does_not_come_back_stops_everything(client, fw):
    fw.bugs |= {"div_zero_dead"}
    job, camp = campaign(client, max_inputs=400)
    assert job["status"] == "FAILED" and "did not come back" in camp["stop_reason"]
    assert "dead" in kinds(camp) and camp["finding_list"][0]["severity"] == "critical"


def test_slow_replies_and_missing_line_timeouts(client, fw):
    fw.bugs |= {"slow_add", "no_line_timeout"}
    job, camp = campaign(client, families=["boundary", "timeout"], max_inputs=60)
    assert {"slow", "no_timeout"} <= kinds(camp)


def test_budgets_and_roles(client, fw):
    r = client.post("/fuzz/campaigns", headers=auth("agent"), json={"max_inputs": 1000})
    assert r.status_code == 422 and "operator" in r.json()["error"]["message"]
    assert client.post("/fuzz/campaigns", headers=auth("agent"), json={"max_resets": 5}).status_code == 422
    assert client.post("/fuzz/campaigns", headers=auth("agent"), json={"families": ["nope"]}).status_code == 422
    assert client.post("/fuzz/campaigns", headers=auth("viewer"), json={}).status_code == 403
    job, camp = campaign(client, role="operator", max_inputs=700, max_resets=5)
    assert camp["budget"]["max_inputs"] == 700


def test_same_seed_same_campaign(client, fw):
    _, a = campaign(client, max_inputs=50)
    first = list(fw.log)
    fw.log.clear()
    _, b = campaign(client, max_inputs=50)
    strip = lambda log: [x for x in log if x not in ("PING", "UPTIME", "VERSION", "RESETINFO")]
    assert strip(first) == strip(fw.log) and a["seed"] == b["seed"] == 5


# ------------------------------------------------------------------ reproduction
def test_reproduce_and_minimise_a_crash_then_see_it_fixed(client, fw):
    fw.bugs |= {"long_arg_crash"}
    _, camp = campaign(client, families=["oversize"], max_inputs=20)
    crash = next(f for f in camp["finding_list"] if f["kind"] == "crash")
    r = client.post(f"/fuzz/findings/{crash['id']}/reproduce", headers=auth("agent"), json={"attempts": 3})
    res = wait_done(client, r.json()["id"], 60)["result"]
    assert res["status"] == "reproduced" and res["hits"] == 3
    assert len(res["minimized"].split(" ")[-1]) == 25          # the bug starts at 25 characters
    f = client.get(f"/fuzz/findings/{crash['id']}", headers=auth("viewer")).json()
    assert f["status"] == "reproduced" and f["minimized"] == res["minimized"]
    assert f["reproductions"][0]["recipe"]["send"] == [res["minimized"]]
    fw.bugs.clear()                                            # "a fixed build"
    r = client.post(f"/fuzz/findings/{crash['id']}/reproduce", headers=auth("agent"), json={"minimise": False})
    assert wait_done(client, r.json()["id"], 60)["result"]["status"] == "not_reproduced"
    assert len(client.get(f"/fuzz/findings/{crash['id']}", headers=auth("viewer")).json()["reproductions"]) == 2


def test_finding_status_is_an_operator_decision(client, fw):
    fw.bugs |= {"div_zero_hang"}
    _, camp = campaign(client, families=["boundary"], max_inputs=200)
    fid = camp["finding_list"][0]["id"]
    assert client.patch(f"/fuzz/findings/{fid}", headers=auth("agent"), json={"status": "wont_fix"}).status_code == 403
    r = client.patch(f"/fuzz/findings/{fid}", headers=auth("operator"), json={"status": "wont_fix"})
    assert r.json()["status"] == "wont_fix"
    assert client.patch(f"/fuzz/findings/{fid}", headers=auth("operator"), json={"status": "x"}).status_code == 422
    listed = client.get("/fuzz/findings?min_severity=high", headers=auth("viewer")).json()
    assert all(f["severity"] in ("critical", "high") for f in listed)


def test_fuzz_tools(client, fw):
    fw.bugs |= {"div_zero_hang"}
    call = lambda name, args, role="agent": client.post(f"/tools/{name}/call", headers=auth(role),
                                                       json={"arguments": args})
    camp = call("start_fuzz", {"project": "calc", "families": ["boundary"], "max_inputs": 200, "seed": 3}).json()
    wait_done(client, camp["result"]["job_id"], 60)
    found = call("list_fuzz_findings", {"campaign_id": camp["result"]["id"], "min_severity": "high"}, "viewer")
    assert found.json()["result"][0]["kind"] == "hang"
    assert call("start_fuzz", {"max_inputs": 600}).status_code == 422
    assert call("start_fuzz", {}, "viewer").status_code == 403
