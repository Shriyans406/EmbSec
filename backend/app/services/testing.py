"""Test execution service: runs lists of "send this command, expect that reply" checks against the board as a job,
and stores the results. (Phase 9 grows this into the full test framework.)
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

from ..db import connect, now_iso
from ..errors import ApiError
from ..hardware.link import LinkError
from ..jobs import JobContext, JobFailed, JobManager
from .device import DeviceManager, validate_command


class Expect(BaseModel):
    """What the reply must look like. Everything you give must hold."""
    equals: Optional[str] = Field(None, description="the whole reply line must be exactly this")
    prefix: Optional[str] = Field(None, description="the reply must start with this")
    contains: Optional[str] = Field(None, description="the reply must contain this text")
    regex: Optional[str] = Field(None, description="the reply must match this regular expression")

    @model_validator(mode="after")
    def _at_least_one(self):
        if not any(v is not None for v in (self.equals, self.prefix, self.contains, self.regex)):
            raise ValueError("give at least one of: equals, prefix, contains, regex")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as e:
                raise ValueError(f"bad regular expression: {e}")
        return self


class TestCase(BaseModel):
    __test__ = False  # not a pytest class
    name: str = Field(min_length=1, max_length=60)
    command: str = Field(min_length=1, max_length=120)
    expect: Expect


class RunRequest(BaseModel):
    suite: Optional[str] = Field(None, description="name of a built-in suite, e.g. 'smoke'")
    cases: Optional[List[TestCase]] = Field(None, max_length=50, description="your own checks")
    repeat: int = Field(1, ge=1, le=20, description="run the whole list this many times")
    name: Optional[str] = Field(None, max_length=60)

    @model_validator(mode="after")
    def _one_source(self):
        if (self.suite is None) == (self.cases is None):
            raise ValueError("give exactly one of: suite, cases")
        return self


def _c(name: str, command: str, **expect) -> dict:
    return {"name": name, "command": command, "expect": expect}


SUITES = {
    "smoke": {
        "description": "Quick, non-destructive health check of the board and its protocol.",
        "cases": [
            _c("handshake", "HELLO", prefix="OK HELLO AEISP-DEV"),
            _c("ping", "PING", equals="OK PONG"),
            _c("version", "VERSION", regex=r"^OK VERSION fw=\S+ proto=1 sdk=\S+ build=\d+ src=\S+$"),
            _c("info", "INFO", prefix="OK INFO name=AEISP-DEV"),
            _c("status", "STATUS", prefix="OK STATUS uptime_ms="),
            _c("echo", "ECHO hello", equals="OK hello"),
            _c("unknown command rejected", "NOPE", prefix="ERR 1 UNKNOWN_CMD"),
            _c("extra argument rejected", "PING now", prefix="ERR 2 BAD_ARGS"),
            _c("blink range checked", "BLINK 20", prefix="ERR 3 BAD_RANGE"),
            _c("led on", "LED ON", equals="OK LED state=ON"),
            _c("led reads back", "LED GET", equals="OK LED state=ON"),
            _c("led off", "LED OFF", equals="OK LED state=OFF"),
            _c("app hook answers", "APP", prefix="ERR 2 BAD_ARGS usage: APP"),
        ],
    },
}


def evaluate(reply: Optional[str], expect: dict) -> Tuple[bool, str]:
    """(passed, why-not). `reply` is None if the board did not answer."""
    if reply is None:
        return False, "no reply"
    if expect.get("equals") is not None and reply != expect["equals"]:
        return False, f"expected exactly {expect['equals']!r}"
    if expect.get("prefix") is not None and not reply.startswith(expect["prefix"]):
        return False, f"expected it to start with {expect['prefix']!r}"
    if expect.get("contains") is not None and expect["contains"] not in reply:
        return False, f"expected it to contain {expect['contains']!r}"
    if expect.get("regex") is not None and not re.search(expect["regex"], reply):
        return False, f"expected it to match /{expect['regex']}/"
    return True, ""


class TestService:
    __test__ = False  # not a pytest class

    def __init__(self, db_path, jobs: JobManager, device: DeviceManager) -> None:
        from ..testfw.suites import SuiteStore

        self.db_path = db_path
        self.jobs = jobs
        self.device = device
        self.suites_db = SuiteStore(db_path)
        self.suites_db.seed_builtin(SUITES)
        jobs.register("test", self._run, "board")

    def suites(self) -> dict:
        return {s["name"]: {"description": s["description"], "cases": s["enabled_cases"], "builtin": s["builtin"],
                            "project": s["project"], "revision": s["revision"]} for s in self.suites_db.list()}

    def start(self, req: RunRequest, requested_by: str) -> dict:
        suite_id = revision = None
        if req.suite is not None:
            try:
                suite = self.suites_db.get(req.suite, enabled_only=True)
            except ApiError:
                raise ApiError(404, "not_found", f"unknown suite {req.suite!r}; available: "
                                                 f"{', '.join(self.suites())}")
            cases = [{"name": c["name"], "command": c["command"], "expect": c["expect"]} for c in suite["cases"]]
            if not cases:
                raise ApiError(409, "empty_suite", f"suite {req.suite!r} has no enabled cases")
            name, suite_id, revision = req.name or req.suite, suite["id"], suite["revision"]
            builtin = suite["builtin"]
        else:
            cases = [c.model_dump(exclude_none=True) for c in req.cases or []]
            name, builtin = req.name or "custom checks", False
        if not builtin:                       # refuse dangerous commands before queueing (built-in suites are our
            for c in cases:                   # own trusted code and may send bad input on purpose)
                validate_command(c["command"], operator=False)
        return self.jobs.submit("test", {"name": name, "cases": cases, "repeat": req.repeat, "suite_id": suite_id,
                                         "suite_revision": revision}, requested_by,
                                summary=f"test run '{name}' ({len(cases)} checks x {req.repeat})")

    @staticmethod
    def _firmware(send) -> dict:
        """What the board runs, from VERSION: fw version, build id and source hash (empty if it cannot be read)."""
        try:
            reply = send("VERSION")
        except LinkError:
            return {}
        fields = dict(re.findall(r"(\w+)=(\S+)", reply or ""))
        build = fields.get("build")
        return {"fw_version": fields.get("fw"), "build_id": int(build) if build and build.isdigit() else None,
                "src_hash": fields.get("src")}

    def _run(self, ctx: JobContext) -> dict:
        p = ctx.params
        started = now_iso()
        t_start = time.perf_counter()
        results, passed, failed, aborted = [], 0, 0, False
        with self.device.session(f"test job #{ctx.job_id}") as send:
            firmware = self._firmware(send)
            if firmware:
                ctx.log(f"board runs fw {firmware.get('fw_version')} build #{firmware.get('build_id')} "
                        f"src {firmware.get('src_hash')}")
            for rep in range(p["repeat"]):
                for c in p["cases"]:
                    t0 = time.perf_counter()
                    reply, err = None, ""
                    try:
                        reply = send(c["command"])
                    except LinkError as e:
                        err = str(e)
                    ms = round((time.perf_counter() - t0) * 1000, 1)
                    ok, why = evaluate(reply, c["expect"])
                    if err:
                        why = err
                    passed += ok
                    failed += (not ok)
                    results.append({"repeat": rep + 1, "name": c["name"], "command": c["command"], "reply": reply,
                                    "passed": ok, "why": why, "latency_ms": ms})
                    ctx.log(f"{'PASS' if ok else 'FAIL'}  {c['name']}: {c['command']!r} -> {reply!r}"
                            + (f"   ({why})" if not ok else ""))
                    if err:  # the board is gone; the remaining checks cannot run
                        aborted = True
                        break
                if aborted:
                    break
        total = passed + failed
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        out = {"name": p["name"], "total": total, "passed": passed, "failed": failed, "aborted": aborted,
               "suite_id": p.get("suite_id"), "suite_revision": p.get("suite_revision"), "firmware": firmware,
               "duration_ms": duration_ms, "cases": results}
        with connect(self.db_path) as c:
            cur = c.execute("INSERT INTO test_runs (job_id, name, passed, failed, total, started_at, finished_at, "
                            "result_json, suite_id, suite_revision, build_id, src_hash, fw_version, duration_ms) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (ctx.job_id, p["name"], passed, failed, total, started, now_iso(), json.dumps(out),
                             p.get("suite_id"), p.get("suite_revision"), firmware.get("build_id"),
                             firmware.get("src_hash"), firmware.get("fw_version"), duration_ms))
            out["run_id"] = int(cur.lastrowid)
        ctx.log(f"result: {passed} passed, {failed} failed" + (" (stopped early: the board stopped answering)" if aborted else ""))
        if aborted:
            planned = len(p["cases"]) * p["repeat"]
            raise JobFailed(f"the board stopped answering ({results[-1]['why']}); only {total} of {planned} checks "
                            f"could run", out)
        if failed:
            raise JobFailed(f"{failed} of {total} checks failed", out)
        return out

    # ------------------------------------------------------------ reading
    def list_runs(self, limit: int = 20) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT id, job_id, name, passed, failed, total, started_at, finished_at, suite_id, "
                             "suite_revision, build_id, src_hash, fw_version, duration_ms "
                             "FROM test_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: int) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no test run {run_id}")
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
        return d