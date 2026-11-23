"""Regression: compare two test runs case by case, and follow a suite across firmware builds.

Each run records the build id and source hash the board reported (VERSION) and the suite revision. A case counts
as passed in a run only if every repeat of it passed. Comparing a run with the latest run of the same suite on an
EARLIER build shows what the new firmware broke (regressed), fixed, or left as it was.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Dict, List, Optional

from ..db import connect
from ..errors import ApiError


def _run(db_path: Path, run_id: int) -> dict:
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
    if not r:
        raise ApiError(404, "not_found", f"no test run {run_id}")
    d = dict(r)
    d["result"] = json.loads(d.pop("result_json") or "{}")
    return d


def case_summary(run: dict) -> Dict[str, dict]:
    """name -> {passed (all repeats), runs, median latency, last reply, why}"""
    out: Dict[str, dict] = {}
    for c in (run.get("result") or {}).get("cases", []):
        s = out.setdefault(c["name"], {"command": c["command"], "passed": True, "runs": 0, "lat": [], "reply": None,
                                       "why": "", "flaky": False})
        s["runs"] += 1
        s["passed"] = s["passed"] and bool(c["passed"])
        s["flaky"] = s["flaky"] or bool(c.get("flaky"))
        if c.get("latency_ms") is not None:
            s["lat"].append(c["latency_ms"])
        s["reply"] = c.get("reply")
        if not c["passed"]:
            s["why"] = c.get("why", "")
    for s in out.values():
        s["median_ms"] = round(statistics.median(s["lat"]), 1) if s["lat"] else None
        del s["lat"]
    return out


def _label(run: dict) -> dict:
    return {"run_id": run["id"], "build_id": run.get("build_id"), "src_hash": run.get("src_hash"),
            "fw_version": run.get("fw_version"), "suite_revision": run.get("suite_revision"),
            "passed": run["passed"], "failed": run["failed"], "total": run["total"], "at": run["started_at"]}


def compare(db_path: Path, baseline_id: int, current_id: int) -> dict:
    base, cur = _run(db_path, baseline_id), _run(db_path, current_id)
    a, b = case_summary(base), case_summary(cur)
    cats: Dict[str, List[dict]] = {k: [] for k in ("regressed", "fixed", "still_failing", "still_passing", "added",
                                                   "removed")}
    for name in sorted(set(a) | set(b)):
        if name not in a:
            cats["added"].append({"name": name, "passed": b[name]["passed"], "reply": b[name]["reply"]})
            continue
        if name not in b:
            cats["removed"].append({"name": name})
            continue
        was, now = a[name]["passed"], b[name]["passed"]
        item = {"name": name, "command": b[name]["command"], "before": a[name]["reply"], "after": b[name]["reply"],
                "why": b[name]["why"], "median_ms_before": a[name]["median_ms"], "median_ms_after": b[name]["median_ms"]}
        key = ("still_passing" if now else "regressed") if was else ("fixed" if now else "still_failing")
        cats[key].append(item)
    slower = [x for x in cats["still_passing"] if x["median_ms_before"] and x["median_ms_after"]
              and x["median_ms_after"] > 2 * x["median_ms_before"] + 5]
    verdict = "regression" if cats["regressed"] else ("improved" if cats["fixed"] else "same")
    notes = []
    if base.get("suite_id") != cur.get("suite_id"):
        notes.append("the two runs are of different suites")
    elif base.get("suite_revision") != cur.get("suite_revision"):
        notes.append("the suite changed between the runs (different revision)")
    if base.get("build_id") is not None and base.get("build_id") == cur.get("build_id"):
        notes.append("both runs are on the same build")
    return {"verdict": verdict, "baseline": _label(base), "current": _label(cur),
            "counts": {k: len(v) for k, v in cats.items()}, **cats,
            "slower": [{"name": x["name"], "before_ms": x["median_ms_before"], "after_ms": x["median_ms_after"]}
                       for x in slower], "notes": notes}


def runs_of_suite(db_path: Path, suite_id: int, limit: int = 100) -> List[dict]:
    with connect(db_path) as c:
        rows = c.execute("SELECT id, name, passed, failed, total, started_at, suite_revision, build_id, src_hash, "
                         "fw_version, duration_ms FROM test_runs WHERE suite_id = ? ORDER BY id DESC LIMIT ?",
                         (suite_id, limit)).fetchall()
    return [dict(r) for r in rows]


def history(db_path: Path, suite_id: int) -> List[dict]:
    """The latest run of the suite on every build, newest build first: the firmware-version matrix."""
    latest: Dict[Optional[int], dict] = {}
    for r in runs_of_suite(db_path, suite_id, 500):
        latest.setdefault(r["build_id"], r)
    return sorted(latest.values(), key=lambda r: (r["build_id"] or 0), reverse=True)


def regression(db_path: Path, suite_id: int, build_id: Optional[int] = None,
               baseline_build: Optional[int] = None) -> dict:
    """Latest run on `build_id` (default: the newest build tested) against the latest run on an earlier build."""
    by_build = history(db_path, suite_id)
    builds = [r for r in by_build if r["build_id"] is not None]
    if not builds:
        raise ApiError(409, "no_runs", "this suite has no runs with a known build yet")
    current = next((r for r in builds if r["build_id"] == build_id), None) if build_id else builds[0]
    if current is None:
        raise ApiError(404, "not_found", f"this suite was never run on build #{build_id}")
    if baseline_build is not None:
        base = next((r for r in builds if r["build_id"] == baseline_build), None)
        if base is None:
            raise ApiError(404, "not_found", f"this suite was never run on build #{baseline_build}")
    else:
        base = next((r for r in builds if r["build_id"] < current["build_id"]), None)
    if base is None:
        return {"verdict": "no_baseline", "current": current,
                "notes": [f"no run on a build before #{current['build_id']}"]}
    return compare(db_path, base["id"], current["id"])
