"""Benchmarks, their runs, baselines and comparisons (tables benchmarks / benchmark_runs).

compare(a, b) decides, per measurement, whether b is better, worse or the same as a. A difference only counts when
it is larger than the noise of the two runs:
    device time    noise = max(2 x the larger stdev, 2 % of a, 1 us)       lower is better
    host time      noise = max(2 x the larger stdev, 5 % of a, 0.05 ms)    lower is better (USB is noisy)
    throughput     noise = max(2 x the larger stdev, 5 % of a)             higher is better
    flash / RAM    exact numbers from the build: any change counts           lower is better
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..db import connect, now_iso
from ..errors import ApiError
from ..jobs import JobContext, JobFailed
from .bench import DEFAULT_BENCHMARK, BenchError, benchmark_for_spec, check_definition, run_benchmark

NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,47}$")


def _delta(a: Optional[dict], b: Optional[dict], rel: float, floor: float, higher_better: bool = False) -> Optional[dict]:
    if not a or not b:
        return None
    ma, mb = a["median"], b["median"]
    noise = max(2 * max(a.get("stdev", 0), b.get("stdev", 0)), rel * abs(ma), floor)
    diff = mb - ma
    if abs(diff) <= noise:
        verdict = "same"
    elif (diff < 0) != higher_better:
        verdict = "better"
    else:
        verdict = "worse"
    return {"before": ma, "after": mb, "change": round(diff, 3),
            "change_pct": round(100 * diff / ma, 2) if ma else None, "noise": round(noise, 3), "verdict": verdict}


def compare_results(a: dict, b: dict, a_size: dict, b_size: dict) -> dict:
    out = {"commands": {}, "notes": []}
    if a["benchmark"] != b["benchmark"]:
        out["notes"].append("different benchmarks")
    if a.get("definition") != b.get("definition"):
        out["notes"].append("the benchmark definition changed between the runs")
    if not (a.get("device_timing") and b.get("device_timing")):
        out["notes"].append("one of the runs has no on-device timing; only host times are compared")
    for name in sorted(set(a["commands"]) | set(b["commands"])):
        ca, cb = a["commands"].get(name), b["commands"].get(name)
        if not ca or not cb:
            out["commands"][name] = {"missing_in": "before" if not ca else "after"}
            continue
        out["commands"][name] = {"device_us": _delta(ca.get("device_us"), cb.get("device_us"), 0.02, 1.0),
                                 "host_ms": _delta(ca.get("host_ms"), cb.get("host_ms"), 0.05, 0.05),
                                 "errors": [ca["errors"], cb["errors"]]}
    out["throughput_cps"] = _delta(a.get("throughput_cps"), b.get("throughput_cps"), 0.05, 0.0, higher_better=True)
    size = {}
    for k in ("flash_bytes", "ram_bytes"):
        if a_size.get(k) is not None and b_size.get(k) is not None:
            d = b_size[k] - a_size[k]
            size[k] = {"before": a_size[k], "after": b_size[k], "change": d,
                       "verdict": "same" if d == 0 else ("better" if d < 0 else "worse")}
    out["size"] = size
    verdicts = [v["verdict"] for c in out["commands"].values() for v in (c.get("device_us") or c.get("host_ms"),) if v]
    if not verdicts:
        out["speed"] = "unknown"
    elif all(v == "same" for v in verdicts):
        out["speed"] = "same"
    elif "worse" not in verdicts:
        out["speed"] = "faster"
    elif "better" not in verdicts:
        out["speed"] = "slower"
    else:
        out["speed"] = "mixed"
    return out


class ProfilingService:
    def __init__(self, db_path: Path, jobs, device, builds=None, tests=None) -> None:
        self.db_path = Path(db_path)
        self.jobs = jobs
        self.device = device
        self.builds = builds
        self.tests = tests
        self._seed()
        jobs.register("benchmark", self._run, "board")

    # ------------------------------------------------------------ definitions
    def _seed(self) -> None:
        with connect(self.db_path) as c:
            r = c.execute("SELECT id FROM benchmarks WHERE name = ?", (DEFAULT_BENCHMARK["name"],)).fetchone()
            if not r:
                c.execute("INSERT INTO benchmarks (name, description, definition_json, builtin, created_by, "
                          "created_at) VALUES (?, ?, ?, 1, 'builtin', ?)",
                          (DEFAULT_BENCHMARK["name"], DEFAULT_BENCHMARK["description"],
                           json.dumps(DEFAULT_BENCHMARK), now_iso()))

    @staticmethod
    def _bench(r) -> dict:
        d = dict(r)
        d["definition"] = json.loads(d.pop("definition_json"))
        d["builtin"] = bool(d["builtin"])
        return d

    def get(self, name: str) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM benchmarks WHERE name = ?", (name,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no benchmark {name!r}")
        return self._bench(r)

    def list(self, project: Optional[str] = None) -> list:
        sql, args = "SELECT * FROM benchmarks", ()
        if project:
            sql, args = sql + " WHERE project = ?", (project,)
        with connect(self.db_path) as c:
            return [self._bench(r) for r in c.execute(sql + " ORDER BY name", args).fetchall()]

    def save(self, definition: dict, who: str, replace: bool = False) -> dict:
        name = definition.get("name", "")
        if not NAME_RE.match(name):
            raise ApiError(422, "bad_name", f"{name!r}: lower-case letters, digits, '_' or '-' (2..48)")
        try:
            d = check_definition(json.loads(json.dumps(definition)))
        except BenchError as e:
            raise ApiError(422, "bad_benchmark", str(e))
        with connect(self.db_path) as c:
            old = c.execute("SELECT * FROM benchmarks WHERE name = ?", (name,)).fetchone()
            if old and old["builtin"]:
                raise ApiError(403, "builtin_benchmark", f"{name!r} is built in and cannot be changed")
            if old and not replace:
                raise ApiError(409, "benchmark_exists", f"a benchmark called {name!r} exists already")
            if old:
                c.execute("UPDATE benchmarks SET project=?, description=?, definition_json=?, baseline_run_id=NULL, "
                          "updated_at=? WHERE id=?", (d.get("project"), d.get("description", ""), json.dumps(d),
                                                      now_iso(), old["id"]))
            else:
                c.execute("INSERT INTO benchmarks (name, project, description, definition_json, created_by, "
                          "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (name, d.get("project"), d.get("description", ""), json.dumps(d), who, now_iso()))
        return self.get(name)

    def for_project(self, project: str, who: str) -> dict:
        """The project's benchmark, made from its specification the first time."""
        name = f"bench-{project}".replace("_", "-")[:48]
        try:
            return self.get(name)
        except ApiError:
            spec = self.tests.latest_spec(project) if self.tests else None
            if not spec:
                raise ApiError(404, "no_spec", f"project {project!r} has no specification to build a benchmark from")
            return self.save(benchmark_for_spec(project, spec), who)

    # ------------------------------------------------------------ running
    def start(self, name: str, who: str) -> dict:
        b = self.get(name)
        return self.jobs.submit("benchmark", {"benchmark": b["name"]}, who,
                                summary=f"benchmark {b['name']} ({b['definition']['runs']} runs)")

    def _size(self, build_id: Optional[int]) -> dict:
        if not build_id or not self.builds:
            return {}
        try:
            b = self.builds.get(build_id)
        except ApiError:
            return {}
        return {"flash_bytes": b.get("flash_bytes"), "ram_bytes": b.get("ram_bytes")}

    def _run(self, ctx: JobContext) -> dict:
        b = self.get(ctx.params["benchmark"])
        with self.device.session(f"benchmark {b['name']}") as send:
            try:
                res = run_benchmark(send, b["definition"], ctx.log)
            except BenchError as e:
                raise JobFailed(str(e))
        fw = res["firmware"]
        size = self._size(fw.get("build_id"))
        res["size"] = size
        with connect(self.db_path) as c:
            cur = c.execute("INSERT INTO benchmark_runs (benchmark_id, job_id, build_id, src_hash, fw_version, "
                            "device_timing, repeatable, flash_bytes, ram_bytes, result_json, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (b["id"], ctx.job_id, fw.get("build_id"), fw.get("src_hash"), fw.get("fw_version"),
                             int(res["device_timing"]), int(res["repeatable"]), size.get("flash_bytes"),
                             size.get("ram_bytes"), json.dumps(res, default=str), now_iso()))
            run_id = int(cur.lastrowid)
        summary = ", ".join(f"{n} {c['device_us']['median'] if c['device_us'] else c['host_ms']['median']}"
                            f"{' us' if c['device_us'] else ' ms (host)'}" for n, c in res["commands"].items())
        ctx.log(f"benchmark run #{run_id} on build #{fw.get('build_id')}: {summary}; "
                f"{'repeatable' if res['repeatable'] else 'NOT repeatable'}")
        return {"run_id": run_id, "build_id": fw.get("build_id"), "repeatable": res["repeatable"],
                "device_timing": res["device_timing"]}

    # ------------------------------------------------------------ runs, baselines, history
    @staticmethod
    def _runrow(r) -> dict:
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json"))
        d["device_timing"], d["repeatable"] = bool(d["device_timing"]), bool(d["repeatable"])
        return d

    def run(self, run_id: int) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM benchmark_runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no benchmark run {run_id}")
        return self._runrow(r)

    def runs(self, name: str, build_id: Optional[int] = None, limit: int = 50) -> list:
        b = self.get(name)
        sql, args = "SELECT * FROM benchmark_runs WHERE benchmark_id = ?", [b["id"]]
        if build_id is not None:
            sql += " AND build_id = ?"
            args.append(build_id)
        with connect(self.db_path) as c:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
        return [self._runrow(r) for r in rows]

    def set_baseline(self, name: str, run_id: int) -> dict:
        b, run = self.get(name), self.run(run_id)
        if run["benchmark_id"] != b["id"]:
            raise ApiError(422, "wrong_benchmark", f"run {run_id} belongs to another benchmark")
        if not run["repeatable"]:
            raise ApiError(409, "not_repeatable", f"run {run_id} is not repeatable; a baseline must be")
        with connect(self.db_path) as c:
            c.execute("UPDATE benchmarks SET baseline_run_id = ?, updated_at = ? WHERE id = ?",
                      (run_id, now_iso(), b["id"]))
        return self.get(name)

    def compare(self, before_id: int, after_id: int) -> dict:
        a, b = self.run(before_id), self.run(after_id)
        out = compare_results(a["result"], b["result"], a["result"].get("size") or {}, b["result"].get("size") or {})
        out.update({"before": {"run_id": a["id"], "build_id": a["build_id"]},
                    "after": {"run_id": b["id"], "build_id": b["build_id"]}})
        return out

    def against_baseline(self, name: str, run_id: Optional[int] = None) -> dict:
        b = self.get(name)
        if not b["baseline_run_id"]:
            raise ApiError(409, "no_baseline", f"benchmark {name!r} has no baseline yet")
        run_id = run_id or (self.runs(name, limit=1) or [{}])[0].get("id")
        if not run_id:
            raise ApiError(409, "no_runs", f"benchmark {name!r} was never run")
        return self.compare(b["baseline_run_id"], run_id)

    def history(self, name: str) -> dict:
        b = self.get(name)
        rows = []
        for r in self.runs(name, limit=200):
            res = r["result"]
            rows.append({"run_id": r["id"], "build_id": r["build_id"], "at": r["created_at"],
                         "repeatable": r["repeatable"], "device_timing": r["device_timing"],
                         "flash_bytes": r["flash_bytes"], "ram_bytes": r["ram_bytes"],
                         "throughput_cps": (res.get("throughput_cps") or {}).get("median"),
                         "commands": {n: (c["device_us"] or {}).get("median") if c.get("device_us")
                                      else (c["host_ms"] or {}).get("median") for n, c in res["commands"].items()},
                         "baseline": r["id"] == b["baseline_run_id"]})
        return {"benchmark": name, "baseline_run_id": b["baseline_run_id"], "runs": rows}
