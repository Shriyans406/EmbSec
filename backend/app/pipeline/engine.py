"""The pipeline engine: a plain state machine that moves one requirement through the stages.

    spec  -> code -> build --(failed: diagnostics)--> code (fix round) ...
                       \-> scan --(FAIL: findings)--> code (fix round) ...
                              \-> report -> done

Models are used where judgement is needed (writing the spec, writing the code); everything that decides whether a
stage passed is plain code: the compiler, the security scanner, the test runner. The number of fix rounds is
limited. Each stage records events, so a pipeline can be followed and audited step by step.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from ..ai import prompts
from ..build.engine import SUCCESS as BUILD_SUCCESS
from ..db import now_iso
from ..errors import ApiError
from ..jobs import JobContext, JobFailed
from .spec import spec_for_coder, write_spec
from .store import (BUILD, CODE, DONE, FAILED, FINISHED, REPORT, RUNNING, SCAN, SPEC, STOPPED, PipelineStore)

log = logging.getLogger("aeisp.pipeline")

WHO = "pipeline"


class StageFailed(Exception):
    def __init__(self, message: str, outcome: str = "failed") -> None:
        super().__init__(message)
        self.message = message
        self.outcome = outcome


class PipelineEngine:
    def __init__(self, services, settings) -> None:
        self.s = services
        self.settings = settings
        self.store = PipelineStore(settings.db_path)
        self.store.recover_orphans()
        self.handlers = {SPEC: self.stage_spec, CODE: self.stage_code, BUILD: self.stage_build,
                         SCAN: self.stage_scan, REPORT: self.stage_report}
        services.jobs.register("pipeline", self._run_job, "ai")

    # ------------------------------------------------------------ starting
    def start(self, requirement: str, requested_by: str, max_fix_rounds: int = 3) -> dict:
        if not self.s.agents.configured:
            raise ApiError(503, "ai_not_configured", "GEMINI_API_KEY is not set in .env; the pipeline cannot run")
        p = self.store.create(requirement, requested_by, {"max_fix_rounds": max_fix_rounds})
        job = self.s.jobs.submit("pipeline", {"pipeline_id": p["id"]}, requested_by,
                                 summary=f"pipeline {p['id']}: {requirement[:80]}")
        return {**self.store.get(p["id"]), "job_id": job["id"]}

    def get(self, pid: int) -> dict:
        p = self.store.get(pid)
        if p is None:
            raise ApiError(404, "not_found", f"no pipeline {pid}")
        return p

    # ------------------------------------------------------------ the loop
    def _run_job(self, ctx: JobContext) -> dict:
        return self.drive(ctx.params["pipeline_id"], ctx.log)

    def drive(self, pid: int, log_fn: Callable[[str], None]) -> dict:
        self.store.update(pid, status=RUNNING)
        while True:
            p = self.get(pid)
            stage = p["stage"]
            if stage == DONE:
                break
            if p["stop_requested"] and stage != REPORT:
                self._event(p, "stop", "stopped by an operator")
                self.store.update(pid, stage=REPORT, outcome="stopped", error="stopped by an operator")
                continue
            try:
                nxt = self.handlers[stage](p, log_fn)
            except StageFailed as e:
                self._event(p, "failed", e.message)
                self.store.update(pid, stage=REPORT, outcome=e.outcome, error=e.message)
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("pipeline %s crashed in %s", pid, stage)
                self._event(p, "crash", f"{type(e).__name__}: {e}")
                self.store.update(pid, stage=REPORT, outcome="failed", error=f"{stage}: {type(e).__name__}: {e}")
                continue
            if nxt is None:                      # paused (waiting for a human)
                return {"pipeline_id": pid, "status": self.get(pid)["status"], "stage": self.get(pid)["stage"]}
            self.store.update(pid, stage=nxt)
        p = self.get(pid)
        out = {"pipeline_id": pid, "status": p["status"], "outcome": p["outcome"], "build_id": p["build_id"]}
        if p["outcome"] not in ("success",):
            raise JobFailed(f"pipeline {pid} ended: {p['outcome']} - {p['error'] or ''}".strip(" -"), out)
        return out

    def _event(self, p: dict, kind: str, message: str, detail: Optional[dict] = None, log_fn=None) -> None:
        self.store.event(p["id"], p["stage"], kind, message, detail)
        if log_fn:
            log_fn(f"[{p['stage']}] {message}")

    def _platform(self) -> str:
        return prompts.load("platform").render(max_steps=self.settings.ai_max_steps).strip()

    # ------------------------------------------------------------ stages
    def stage_spec(self, p: dict, log_fn) -> str:
        spec, problems, tokens = write_spec(self.s.agents.llm(), p["requirement"], self._platform())
        self.store.set_state(p["id"], spec_tokens=tokens)
        if problems:
            raise StageFailed("the model could not write a usable specification: " + "; ".join(problems[:5]))
        self.store.update(p["id"], spec_json=json.dumps(spec), project=spec["project"])
        n = sum(len(c["examples"]) for c in spec["commands"])
        self._event(p, "done", f"spec '{spec['title']}': {len(spec['commands'])} commands, {n} examples",
                    {"commands": [c["usage"] for c in spec["commands"]]}, log_fn)
        return CODE

    def stage_code(self, p: dict, log_fn) -> str:
        spec, state = p["spec"], p["state"]
        try:
            self.s.firmware.list_sources(spec["project"])
        except ApiError as e:
            if e.status != 404:
                raise
            self.s.firmware.create_project(spec["project"])
            self._event(p, "info", f"created project {spec['project']}", log_fn=log_fn)
        task = spec_for_coder(spec)
        if state.get("fix_task"):
            task = state["fix_task"] + "\n\n--- The specification (unchanged) ---\n" + task
        res = self.s.agents.run_inline("firmware", task, f"pipeline:{p['id']}", {"pipeline_id": p["id"]}, log_fn)
        runs = state.get("agent_runs", []) + [{"run_id": res["child_run_id"], "agent": "firmware",
                                               "outcome": res["outcome"], "tokens": res.get("tokens", 0)}]
        self.store.set_state(p["id"], agent_runs=runs, fix_task=None)
        self._event(p, "done" if res["outcome"] in ("success", "partial") else "warning",
                    f"firmware agent run {res['child_run_id']}: {res['outcome']}",
                    {"summary": (res.get("report") or {}).get("summary")}, log_fn)
        if res["status"] == "FAILED":
            raise StageFailed(f"the firmware agent failed: {res.get('error')}")
        return BUILD

    def _fix_round(self, p: dict, why: str, task: str, log_fn) -> str:
        rounds = p["fix_rounds"] + 1
        limit = p["params"].get("max_fix_rounds", 3)
        if rounds > limit:
            raise StageFailed(f"{why}, and {limit} fix rounds did not solve it")
        self.store.update(p["id"], fix_rounds=rounds)
        self.store.set_state(p["id"], fix_task=task)
        self._event(p, "fix", f"{why}: fix round {rounds} of {limit}", log_fn=log_fn)
        return CODE

    def stage_build(self, p: dict, log_fn) -> str:
        job = self.s.builds.start(p["project"], WHO)
        job = self.s.jobs.wait(job["id"], 1200)
        res = job.get("result") or {}
        builds = p["state"].get("builds", []) + [{"build_id": res.get("id"), "status": res.get("status"),
                                                  "errors": res.get("errors"), "warnings": res.get("warnings")}]
        self.store.set_state(p["id"], builds=builds)
        if res.get("status") == BUILD_SUCCESS:
            self.store.update(p["id"], build_id=res["id"])
            self._event(p, "done", f"build #{res['id']} SUCCESS ({res.get('flash_bytes')} bytes flash)",
                        {"build_id": res["id"]}, log_fn)
            return SCAN
        if res.get("status") != "FAILED":                 # the toolchain itself broke: not the code's fault
            raise StageFailed(f"the build could not run: {job.get('error')}")
        errors = [d for d in res.get("diagnostics", []) if d.get("severity") == "error"][:15]
        text = "\n".join(f"- {d.get('file')}:{d.get('line')}: {d.get('message')}" for d in errors) or job.get("error")
        return self._fix_round(p, f"build #{res.get('id')} failed with {res.get('errors')} errors",
                               f"The last build (#{res.get('id')}) FAILED. Fix exactly these compiler errors:\n{text}",
                               log_fn)

    def stage_scan(self, p: dict, log_fn) -> str:
        scan = self.s.analysis.security_scan(build_id=p["build_id"])
        self.store.set_state(p["id"], scan={"verdict": scan["verdict"], "counts": scan["counts"]})
        if scan["verdict"] == "PASS":
            self._event(p, "done", f"security scan of build #{p['build_id']}: PASS", scan["counts"], log_fn)
            return self.after_scan(p)
        blocking = [f for f in scan["findings"] if f["severity"] in ("critical", "high")]
        text = "\n".join(f"- {f['file']}:{f['line']} [{f['rule']} {f['severity']}] {f['title']} -> {f['advice']}"
                         for f in blocking[:15])
        return self._fix_round(p, f"build #{p['build_id']} fails the security scan",
                               f"The security scan of build #{p['build_id']} FAILED. Fix these problems:\n{text}",
                               log_fn)

    def after_scan(self, p: dict) -> str:
        return REPORT

    def stage_report(self, p: dict, log_fn) -> str:
        outcome = p["outcome"] or "success"
        report = {"outcome": outcome, "project": p["project"], "build_id": p["build_id"],
                  "fix_rounds": p["fix_rounds"], "error": p["error"], "builds": p["state"].get("builds", [])}
        status = STOPPED if outcome == "stopped" else (FINISHED if outcome != "failed" else FAILED)
        self.store.update(p["id"], report_json=json.dumps(report), outcome=outcome, status=status,
                          finished_at=now_iso())
        self._event(p, "done", f"pipeline finished: {outcome}", log_fn=log_fn)
        return DONE
