"""The benchmark runner and its statistics.

A benchmark is a list of commands, each sent `repeat` times per run, and `runs` runs after `warmup` passes:

    {"name": "bench-calc", "commands": [{"name": "add", "command": "APP ADD 2 3", "repeat": 20}, ...],
     "runs": 5, "warmup": 2, "burst": {"command": "PING", "count": 50}}

Per command and run it records
    device_us   the harness's own measurement of the command (PROF RESET before, PROF CMD <verb> after): the
                firmware's execution time without USB and host overhead. Only on harness 0.4.0+.
    host_ms     the round trip seen by the host (USB + host scheduling + firmware), every single send
and per run the burst throughput (commands per second) and the harness's loop / busy / buffer figures.

Across runs it gives median, p95, mean, standard deviation and the coefficient of variation (CV = stdev / mean).
A benchmark is called repeatable when the device-side medians vary by at most 10 % between runs (host round trips:
25 %, USB timing is noisier) - only repeatable numbers are good baselines.
"""
from __future__ import annotations

import math
import statistics
import time
from typing import Callable, Dict, List, Optional

from ..hardware.link import LinkError
from ..hardware.protocol import parse_reply
from ..services.device import validate_command

DEVICE_CV_LIMIT = 0.10
HOST_CV_LIMIT = 0.25
LIMITS = {"commands": 12, "repeat": 100, "runs": 20, "warmup": 10, "burst": 200}

DEFAULT_BENCHMARK = {
    "name": "harness-basic", "description": "the harness's own commands",
    "commands": [{"name": "ping", "command": "PING", "repeat": 20},
                 {"name": "version", "command": "VERSION", "repeat": 10},
                 {"name": "status", "command": "STATUS", "repeat": 10},
                 {"name": "echo", "command": "ECHO benchmark", "repeat": 10}],
    "runs": 5, "warmup": 2, "burst": {"command": "PING", "count": 50},
}


class BenchError(Exception):
    pass


def check_definition(d: dict) -> dict:
    cmds = d.get("commands") or []
    if not 1 <= len(cmds) <= LIMITS["commands"]:
        raise BenchError(f"a benchmark needs 1..{LIMITS['commands']} commands")
    names = set()
    for c in cmds:
        if not c.get("name") or c["name"] in names:
            raise BenchError("every command needs its own name")
        names.add(c["name"])
        validate_command(c["command"])                    # only safe commands are ever measured
        if c["command"].split()[0] == "PROF":
            raise BenchError("PROF cannot be benchmarked (it is the measuring tool)")
        c.setdefault("repeat", 10)
        if not 1 <= int(c["repeat"]) <= LIMITS["repeat"]:
            raise BenchError(f"repeat must be 1..{LIMITS['repeat']}")
    d.setdefault("runs", 5)
    d.setdefault("warmup", 2)
    if not 2 <= int(d["runs"]) <= LIMITS["runs"]:
        raise BenchError(f"runs must be 2..{LIMITS['runs']} (one run says nothing about repeatability)")
    if not 0 <= int(d["warmup"]) <= LIMITS["warmup"]:
        raise BenchError(f"warmup must be 0..{LIMITS['warmup']}")
    b = d.get("burst")
    if b:
        validate_command(b["command"])
        if not 5 <= int(b.get("count", 50)) <= LIMITS["burst"]:
            raise BenchError(f"burst count must be 5..{LIMITS['burst']}")
        b.setdefault("count", 50)
    return d


# ---------------------------------------------------------------- statistics
def stats(values: List[float]) -> Optional[dict]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mean = statistics.fmean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    p95 = vals[min(len(vals) - 1, int(math.ceil(0.95 * len(vals))) - 1)]
    return {"n": len(vals), "median": round(statistics.median(vals), 3), "p95": round(p95, 3),
            "mean": round(mean, 3), "stdev": round(sd, 3), "min": round(vals[0], 3), "max": round(vals[-1], 3),
            "cv": round(sd / mean, 4) if mean else 0.0}


def _fields(reply: str) -> dict:
    return dict(kv.split("=", 1) for kv in (reply or "").split() if "=" in kv)


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- running
def run_benchmark(send: Callable[[str], str], definition: dict, log: Callable[[str], None] = lambda t: None,
                  clock=time.perf_counter) -> dict:
    d = check_definition(dict(definition))
    version = send("VERSION")
    fw = _fields(version)
    prof_ok = send("PROF RESET").startswith("OK")
    if not prof_ok:
        log("this harness has no on-device profiler (PROF): only host-side timing is measured")
    for _ in range(int(d["warmup"])):
        for c in d["commands"]:
            send(c["command"])
    per: Dict[str, dict] = {c["name"]: {"command": c["command"], "host_ms": [], "device_avg_us": [],
                                        "device_max_us": [], "errors": 0, "replies": set()} for c in d["commands"]}
    runs_sys: List[dict] = []
    burst_rates: List[float] = []
    for run in range(int(d["runs"])):
        for c in d["commands"]:
            p = per[c["name"]]
            if prof_ok:
                send("PROF RESET")
            for _ in range(int(c["repeat"])):
                t0 = clock()
                try:
                    reply = send(c["command"])
                except LinkError as e:
                    raise BenchError(f"the board stopped answering during the benchmark: {e}")
                p["host_ms"].append((clock() - t0) * 1000)
                if not parse_reply(reply).ok:
                    p["errors"] += 1
                if len(p["replies"]) < 3:
                    p["replies"].add(reply)
            if prof_ok:
                f = _fields(send(f"PROF CMD {c['command'].split()[0]}"))
                p["device_avg_us"].append(_num(f.get("avg_us")))
                p["device_max_us"].append(_num(f.get("max_us")))
        b = d.get("burst")
        if b:
            t0 = clock()
            for _ in range(int(b["count"])):
                send(b["command"])
            dt = clock() - t0
            burst_rates.append(b["count"] / dt if dt > 0 else None)
        if prof_ok:
            sys_f = _fields(send("PROF"))
            runs_sys.append({k: _num(v) for k, v in sys_f.items()})
        log(f"run {run + 1}/{d['runs']} done")
    commands = {}
    for name, p in per.items():
        dev = stats(p["device_avg_us"]) if prof_ok else None
        host = stats(p["host_ms"])
        commands[name] = {"command": p["command"], "errors": p["errors"], "replies": sorted(p["replies"]),
                          "device_us": dev, "device_max_us": stats(p["device_max_us"]) if prof_ok else None,
                          "host_ms": host,
                          "repeatable": (dev is not None and dev["cv"] <= DEVICE_CV_LIMIT) or
                                        (dev is None and host is not None and host["cv"] <= HOST_CV_LIMIT)}
    system = None
    if runs_sys:
        keys = ("busy_pct", "loop_max_us", "loop_avg_us", "tick_max_us", "app_tick_avg_us", "rx_hwm", "reply_hwm")
        system = {k: stats([r.get(k) for r in runs_sys]) for k in keys}
    return {
        "benchmark": d["name"], "definition": {k: d[k] for k in ("commands", "runs", "warmup") if k in d} | (
            {"burst": d["burst"]} if d.get("burst") else {}),
        "firmware": {"fw_version": fw.get("fw"), "build_id": int(fw["build"]) if fw.get("build", "").isdigit() else None,
                     "src_hash": fw.get("src")},
        "device_timing": prof_ok, "commands": commands, "throughput_cps": stats(burst_rates), "system": system,
        "repeatable": all(c["repeatable"] for c in commands.values()),
        "errors": sum(c["errors"] for c in commands.values()),
    }


def benchmark_for_spec(project: str, spec: dict, repeat: int = 20) -> dict:
    """One benchmark command per spec command: its first example that is expected to succeed."""
    cmds = []
    for c in spec.get("commands", []):
        for ex in c.get("examples", []):
            e = ex.get("expect") or {}
            ok = e.get("kind") != "error" and str(e.get("value", "OK")).startswith("OK")
            if ok:
                cmds.append({"name": c["name"].lower(), "command": ex["request"], "repeat": repeat})
                break
    cmds.append({"name": "ping", "command": "PING", "repeat": repeat})
    return {"name": f"bench-{project}".replace("_", "-")[:48], "project": project,
            "description": f"one successful example per command of {project}'s specification",
            "commands": cmds[: LIMITS["commands"]], "runs": 5, "warmup": 2,
            "burst": {"command": cmds[0]["command"], "count": 50}}
