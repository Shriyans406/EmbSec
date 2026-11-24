"""Profiling and benchmarks against the simulated board (harness 0.4.0 timings) and an old 0.3.0 board."""
import json

import pytest

from app.pipeline.store import PipelineStore
from app.profiling.bench import BenchError, benchmark_for_spec, check_definition, run_benchmark, stats
from app.profiling.service import compare_results
from app.services.device import DeviceManager

from conftest import auth
from deploy_fakes import add_build
from sim_board import SimFirmware, SimLink
from test_api_services import wait_done

BENCH = {"name": "bench-calc", "project": "calc",
         "commands": [{"name": "mul", "command": "APP MUL 2 3", "repeat": 20},
                      {"name": "ping", "command": "PING", "repeat": 10}],
         "runs": 4, "warmup": 1, "burst": {"command": "PING", "count": 20}}


@pytest.fixture()
def fw(services):
    firmware = SimFirmware(builds={2: {"mul_slow"}, 3: {"mul_fast"}, 4: {"mul_noisy"}})
    services.device = DeviceManager(link_factory=lambda: SimLink(firmware))
    for svc in (services.tests, services.analysis, services.fuzz, services.profiling, services.deploys):
        svc.device = services.device
    return firmware


def run(client, name="bench-calc"):
    r = client.post(f"/benchmarks/{name}/run", headers=auth("agent"))
    assert r.status_code == 202, r.text
    job = wait_done(client, r.json()["id"], 60)
    assert job["status"] == "SUCCEEDED", job["error"]
    return job["result"]["run_id"]


# ------------------------------------------------------------------ pieces
def test_statistics():
    s = stats([10, 12, 11, 13, 50])
    assert s["median"] == 12 and s["p95"] == 50 and s["min"] == 10 and s["max"] == 50 and s["n"] == 5
    assert stats([5, 5, 5])["cv"] == 0.0 and stats([]) is None and stats([None, 3])["n"] == 1


def test_definitions_are_checked():
    check_definition(dict(BENCH))
    for bad, why in [({"name": "x", "commands": []}, "1..12"),
                     ({"name": "x", "commands": [{"name": "a", "command": "PROF"}]}, "measuring tool"),
                     ({"name": "x", "commands": [{"name": "a", "command": "PING"}], "runs": 1}, "repeatability"),
                     ({"name": "x", "commands": [{"name": "a", "command": "PING"}, {"name": "a", "command": "STATUS"}]},
                      "own name")]:
        with pytest.raises(BenchError, match=why):
            check_definition(bad)
    with pytest.raises(Exception):
        check_definition({"name": "x", "commands": [{"name": "a", "command": "BOOTSEL"}]})


def test_the_runner_on_the_simulated_harness():
    fw = SimFirmware()
    res = run_benchmark(SimLink(fw).request, dict(BENCH))
    mul = res["commands"]["mul"]
    assert res["device_timing"] and res["repeatable"] and res["errors"] == 0
    assert mul["device_us"]["median"] == 40 and mul["device_us"]["n"] == 4 and mul["host_ms"]["n"] == 80
    assert res["firmware"] == {"fw_version": "0.4.0", "build_id": 1, "src_hash": "aaaa1111"}
    assert res["throughput_cps"]["n"] == 4 and res["system"]["rx_hwm"]["median"] >= 11
    log = [x for x in fw.log if x.startswith("PROF")]
    assert log.count("PROF RESET") == 1 + 4 * 2                # once at the start, then before each command & run


def test_an_old_harness_gives_host_timing_only():
    res = run_benchmark(SimLink(SimFirmware(fw="0.3.0")).request, dict(BENCH))
    assert res["device_timing"] is False and res["commands"]["mul"]["device_us"] is None
    assert res["commands"]["mul"]["host_ms"]["n"] == 80 and res["system"] is None


def test_noisy_timing_is_not_repeatable():
    res = run_benchmark(SimLink(SimFirmware(bugs={"mul_noisy"})).request, dict(BENCH))
    assert res["commands"]["mul"]["repeatable"] is False and res["repeatable"] is False
    assert res["commands"]["mul"]["device_us"]["cv"] > 0.10


def test_compare_counts_only_differences_above_the_noise():
    base = {"benchmark": "b", "definition": {}, "device_timing": True, "throughput_cps": None,
            "commands": {"m": {"device_us": {"median": 100, "stdev": 1}, "host_ms": {"median": 1, "stdev": 0.01},
                               "errors": 0}}}
    same = json.loads(json.dumps(base))
    same["commands"]["m"]["device_us"]["median"] = 101.5             # inside max(2*1, 2 % of 100)
    faster = json.loads(json.dumps(base))
    faster["commands"]["m"]["device_us"]["median"] = 60
    assert compare_results(base, same, {}, {})["speed"] == "same"
    out = compare_results(base, faster, {"flash_bytes": 1000}, {"flash_bytes": 900})
    assert out["speed"] == "faster" and out["commands"]["m"]["device_us"]["change_pct"] == -40.0
    assert out["size"]["flash_bytes"] == {"before": 1000, "after": 900, "change": -100, "verdict": "better"}


# ------------------------------------------------------------------ through the API
def test_benchmarks_baselines_and_history(client, services, fw):
    assert client.post("/benchmarks", headers=auth("agent"), json=BENCH).status_code == 201
    assert client.post("/benchmarks", headers=auth("agent"), json=BENCH).status_code == 409
    assert client.post("/benchmarks", headers=auth("viewer"), json=BENCH).status_code == 403
    add_build(services, 1, project="calc")
    first = run(client)
    r = client.get(f"/benchmark-runs/{first}", headers=auth("viewer")).json()
    assert r["build_id"] == 1 and r["repeatable"] and r["flash_bytes"] is not None
    assert client.post("/benchmarks/bench-calc/baseline", headers=auth("agent"), json={"run_id": first}).status_code == 403
    assert client.post("/benchmarks/bench-calc/baseline", headers=auth("operator"),
                       json={"run_id": first}).json()["baseline_run_id"] == first
    fw.flash(2)                                                 # a slower MUL
    run(client)
    vs = client.get("/benchmarks/bench-calc/vs-baseline", headers=auth("viewer")).json()
    assert vs["speed"] == "slower" and vs["commands"]["mul"]["device_us"]["verdict"] == "worse"
    assert vs["commands"]["ping"]["device_us"]["verdict"] == "same"
    fw.flash(3)                                                 # a faster MUL
    third = run(client)
    cmp = client.get(f"/benchmark-runs/compare?before={first}&after={third}", headers=auth("viewer")).json()
    assert cmp["speed"] == "faster" and cmp["commands"]["mul"]["device_us"]["after"] == 12
    hist = client.get("/benchmarks/bench-calc/history", headers=auth("viewer")).json()["runs"]
    assert [(h["build_id"], h["commands"]["mul"], h["baseline"]) for h in hist] == \
        [(3, 12.0, False), (2, 400.0, False), (1, 40.0, True)]
    fw.flash(4)                                                 # noisy: cannot become a baseline
    noisy = run(client)
    r = client.post("/benchmarks/bench-calc/baseline", headers=auth("operator"), json={"run_id": noisy})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_repeatable"


def test_the_builtin_benchmark_and_one_made_from_a_spec(client, services, fw):
    names = [b["name"] for b in client.get("/benchmarks", headers=auth("viewer")).json()]
    assert "harness-basic" in names
    assert run(client, "harness-basic")
    assert client.post("/benchmarks", headers=auth("agent"),
                       json={**BENCH, "name": "harness-basic", "replace": True}).status_code == 403
    store = PipelineStore(services.settings.db_path)
    p = store.create("calc", "t", {})
    spec = {"commands": [{"name": "MUL", "examples": [
        {"request": "APP MUL 1", "expect": {"kind": "error", "value": "2"}},
        {"request": "APP MUL 2 3", "expect": {"kind": "equals", "value": "OK product=6"}}]}]}
    store.update(p["id"], project="calc", spec_json=json.dumps(spec))
    b = client.post("/benchmarks/for-project/calc", headers=auth("agent")).json()
    assert b["name"] == "bench-calc" and [c["command"] for c in b["definition"]["commands"]] == ["APP MUL 2 3", "PING"]
    assert benchmark_for_spec("x", spec)["burst"]["command"] == "APP MUL 2 3"
    assert client.post("/benchmarks/for-project/nospec", headers=auth("agent")).status_code == 404


def test_the_profiling_tools(client, services, fw):
    call = lambda name, args, role="agent": client.post(f"/tools/{name}/call", headers=auth(role),
                                                       json={"arguments": args})
    r = call("save_benchmark", {"name": "bench-x", "commands": [{"name": "mul", "command": "APP MUL 2 3"}],
                                "runs": 3, "burst_command": "PING"})
    assert r.status_code == 200, r.text
    job = call("run_benchmark", {"benchmark": "bench-x"}).json()["result"]
    rid = wait_done(client, job["id"], 60)["result"]["run_id"]
    got = call("get_benchmark_run", {"run_id": rid}, "viewer").json()["result"]
    assert got["result"]["commands"]["mul"]["device_us"]["median"] == 40
    assert call("benchmark_history", {"benchmark": "bench-x"}, "viewer").json()["result"]["runs"][0]["run_id"] == rid
    assert call("benchmark_vs_baseline", {"benchmark": "bench-x"}, "viewer").status_code == 409     # no baseline
    assert call("run_benchmark", {"benchmark": "bench-x"}, "viewer").status_code == 403
