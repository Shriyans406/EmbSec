"""The optimisation workflow: a plain state machine, like the pipeline and the remediation.

    baseline  the board must run a known build of the project; its benchmark (repeatable) and its correctness suites
              are measured, and the workspace is put back to exactly that build
    ...       later stages: analyse (Performance agent), variant (Firmware agent), build, scan, size gate, propose
              (a human approves), measure (A/B against the baseline), check (correctness, fuzz), decide - accept, or
              roll back (a human approves again) and try another variant
    report    everything, from the records

The model proposes and writes; plain code measures and decides. A variant is kept only if the goal improved by more
than both the noise and `min_gain_pct`, nothing else got measurably worse, and every correctness check still holds.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Dict, Optional

from ..db import now_iso
from ..errors import ApiError
from ..jobs import JobContext, JobFailed
from ..services.testing import RunRequest
from .store import (BASELINE, DONE, FAILED, FINAL, FINISHED, GOALS, QUEUED, REPORT, RUNNING, STOPPED, WAITING,
                    OptimizationStore)

log = logging.getLogger("aeisp.optimize")


class StageFailed(Exception):
    def __init__(self, message: str, outcome: str = "failed") -> None:
        super().__init__(message)
        self.message, self.outcome = message, outcome


class OptimizationEngine:
    def __init__(self, services, settings) -> None:
        self.s = services
        self.settings = settings
        self.store = OptimizationStore(settings.db_path)
        self.handlers: Dict[str, Callable] = {BASELINE: self.stage_baseline, REPORT: self.stage_report}
        services.jobs.register("optimization", self._run_job, "ai")

    def recover(self) -> int:
        return self.store.recover_orphans()

    # ------------------------------------------------------------ starting
    def start(self, project: str, who: str, goal: str = "speed", target: Optional[str] = None,
              benchmark: Optional[str] = None, min_gain_pct: float = 10.0, max_variants: int = 2,
              fuzz_check: bool = True) -> dict:
        if not self.s.agents.configured:
            raise ApiError(503, "ai_not_configured", "GEMINI_API_KEY is not set in .env; optimisation cannot run")
        if goal not in GOALS:
            raise ApiError(422, "bad_goal", f"goal must be one of {GOALS}")
        if not 1 <= min_gain_pct <= 90 or not 1 <= max_variants <= 3:
            raise ApiError(422, "bad_limits", "min_gain_pct 1..90, max_variants 1..3")
        self.s.firmware.list_sources(project)                         # 404 for an unknown project
        active = self.store.active_for(project)
        if active:
            raise ApiError(409, "already_running", f"optimisation {active['id']} already works on {project}")
        o = self.store.create(project, goal, target, min_gain_pct, who,
                              {"benchmark": benchmark, "max_variants": max_variants, "fuzz_check": fuzz_check})
        job = self.s.jobs.submit("optimization", {"optimization_id": o["id"]}, who,
                                 summary=f"optimise {project} for {goal}" + (f" ({target})" if target else ""))
        return {**self.store.get(o["id"]), "job_id": job["id"]}

    def get(self, oid: int) -> dict:
        return self.store.get(oid)

    # ------------------------------------------------------------ the loop
    def _run_job(self, ctx: JobContext) -> dict:
        return self.drive(ctx.params["optimization_id"], ctx.log)

    def drive(self, oid: int, log_fn: Callable[[str], None]) -> dict:
        self.store.update(oid, status=RUNNING)
        while True:
            o = self.get(oid)
            stage = o["stage"]
            if stage == DONE:
                break
            if o["stop_requested"] and stage != REPORT:
                self.event(o, "stop", "stopped by request")
                self.store.update(oid, stage=REPORT, outcome="stopped", error="stopped by request")
                continue
            try:
                nxt = self.handlers[stage](o, log_fn)
            except StageFailed as e:
                self.event(o, "failed", e.message, log_fn=log_fn)
                self.store.update(oid, stage=REPORT, outcome=e.outcome, error=e.message)
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("optimisation %s crashed in %s", oid, stage)
                self.event(o, "crash", f"{type(e).__name__}: {e}")
                self.store.update(oid, stage=REPORT, outcome="failed", error=f"{stage}: {type(e).__name__}: {e}")
                continue
            if nxt is None:
                return {"optimization_id": oid, "status": self.get(oid)["status"], "stage": self.get(oid)["stage"]}
            self.store.update(oid, stage=nxt)
        o = self.get(oid)
        out = {"optimization_id": oid, "status": o["status"], "outcome": o["outcome"]}
        if o["outcome"] not in ("accepted", "no_gain", "rejected_rolled_back"):
            raise JobFailed(f"optimisation {oid} ended: {o['outcome']} - {o['error'] or ''}".strip(" -"), out)
        return out

    def event(self, o: dict, kind: str, message: str, detail: Optional[dict] = None, log_fn=None) -> None:
        self.store.event(o["id"], o["stage"], kind, message, detail)
        if log_fn:
            log_fn(f"[{o['stage']}] {message}")

    # ------------------------------------------------------------ helpers
    def board_build(self) -> dict:
        with self.s.device.session("optimisation: which build") as send:
            reply = send("VERSION")
        f = dict(kv.split("=", 1) for kv in reply.split() if "=" in kv)
        b = f.get("build", "")
        return {"build_id": int(b) if b.isdigit() else None, "fw_version": f.get("fw"), "src_hash": f.get("src")}

    def run_suites(self, names: list, who: str) -> Dict[str, dict]:
        out = {}
        for name in names:
            job = self.s.jobs.wait(self.s.tests.start(RunRequest(suite=name), who)["id"], 600)
            res = job.get("result") or {}
            out[name] = {"run_id": res.get("run_id"), "passed": res.get("passed"), "failed": res.get("failed"),
                         "total": res.get("total"), "build_id": (res.get("firmware") or {}).get("build_id")}
        return out

    def run_bench(self, name: str, who: str) -> dict:
        job = self.s.jobs.wait(self.s.profiling.start(name, who)["id"], 900)
        if job["status"] != "SUCCEEDED":
            raise StageFailed(f"the benchmark could not run: {job.get('error')}")
        return self.s.profiling.run(job["result"]["run_id"])

    def size_of(self, build_id: int) -> dict:
        b = self.s.builds.get(build_id)
        return {"flash_bytes": b.get("flash_bytes"), "ram_bytes": b.get("ram_bytes")}

    # ------------------------------------------------------------ baseline
    def stage_baseline(self, o: dict, log_fn) -> str:
        fw = self.board_build()
        if not fw["build_id"]:
            raise StageFailed("the board does not report which build it runs", "no_baseline")
        try:
            build = self.s.builds.get(fw["build_id"])
        except ApiError:
            raise StageFailed(f"the board runs build #{fw['build_id']}, which is not in the build history",
                              "no_baseline")
        if build["project"] != o["project"]:
            raise StageFailed(f"the board runs build #{fw['build_id']} of project '{build['project']}', not "
                              f"'{o['project']}': deploy a build of {o['project']} first", "no_baseline")
        snap = {k: v for k, v in self.s.analysis.build_sources(fw["build_id"]).items() if k.endswith((".c", ".h"))}
        if self.s.analysis.project_sources(o["project"]) != snap:
            self.s.firmware.restore_sources(o["project"], fw["build_id"])
            self.event(o, "info", f"the workspace was put back to build #{fw['build_id']} (what the board runs)",
                       log_fn=log_fn)
        name = o["params"].get("benchmark") or self.s.profiling.for_project(o["project"], f"optimisation:{o['id']}")["name"]
        bench = self.run_bench(name, f"optimisation:{o['id']}")
        if not bench["repeatable"] and o["goal"] == "speed":
            bench = self.run_bench(name, f"optimisation:{o['id']}")            # one more try
            if not bench["repeatable"]:
                raise StageFailed("the baseline benchmark is not repeatable (the noise would hide any gain)",
                                  "noisy")
        suites = [s["name"] for s in self.s.tests.suites_db.list(o["project"])] + ["smoke"]
        checks = self.run_suites(suites, f"optimisation:{o['id']}")
        failing = {n: c for n, c in checks.items() if c["failed"]}
        self.store.update(o["id"], base_build=fw["build_id"], base_run_id=bench["id"], benchmark=name)
        self.store.set_state(o["id"], base_suites=checks, base_size=self.size_of(fw["build_id"]),
                             base_firmware=fw, base_failing=list(failing))
        cmds = bench["result"]["commands"]
        self.event(o, "done", f"baseline: build #{fw['build_id']}, benchmark run #{bench['id']} ("
                   + ", ".join(f"{n} {(c['device_us'] or c['host_ms'])['median']}" for n, c in cmds.items())
                   + f"), {sum(c['passed'] or 0 for c in checks.values())} checks pass",
                   {"suites": checks}, log_fn)
        return self.after_baseline(o)

    def after_baseline(self, o: dict) -> str:
        return REPORT

    # ------------------------------------------------------------ report
    def stage_report(self, o: dict, log_fn) -> str:
        outcome = o["outcome"] or "failed"
        status = STOPPED if outcome == "stopped" else (FAILED if outcome == "failed" else FINISHED)
        report = {"outcome": outcome, "error": o["error"], "project": o["project"], "goal": o["goal"],
                  "base_build": o["base_build"], "variant_build": o["variant_build"]}
        self.event(o, "done", f"optimisation finished: {outcome}", log_fn=log_fn)
        self.store.update(o["id"], report_json=json.dumps(report, default=str), outcome=outcome, status=status,
                          finished_at=now_iso())
        return DONE
