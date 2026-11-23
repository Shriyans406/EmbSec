"""The pipeline engine: a plain state machine that moves one requirement through the stages.

    spec -> code -> build --(failed: compiler errors)--> code (fix round)
                      |
                      +-> scan --(FAIL: findings)--> code (fix round)
                           |
                           +-> propose (a human approves or rejects; the pipeline pauses)
                                 |
                                 +-> deployed -> test on the board --(checks fail)--> code (test round)
                                                  |
                                                  +-> report -> done

Models are used where judgement is needed (writing the spec, writing the code); everything that decides whether a
stage passed is plain code: the compiler, the security scanner, the test runner. The number of fix rounds is
limited. Each stage records events, so a pipeline can be followed and audited step by step.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from ..ai import prompts
from ..build.engine import SUCCESS as BUILD_SUCCESS
from ..db import now_iso
from ..errors import ApiError
from ..jobs import AWAITING, JobContext, JobFailed
from ..jobs import FINAL as JOB_FINAL
from ..services.testing import RunRequest, TestCase
from .report import build_report, to_markdown, write_markdown
from .spec import spec_cases, spec_for_coder, write_spec
from .store import (BUILD, CODE, DEPLOYED, DONE, FAILED, FINAL, FINISHED, PROPOSE, QUEUED, REPORT, RUNNING, SCAN,
                    SPEC, STOPPED, TEST, WAITING, PipelineStore)

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
                         SCAN: self.stage_scan, PROPOSE: self.stage_propose, DEPLOYED: self.stage_deployed,
                         TEST: self.stage_test, REPORT: self.stage_report}
        services.jobs.register("pipeline", self._run_job, "ai")

    # ------------------------------------------------------------ starting
    def start(self, requirement: str, requested_by: str, max_fix_rounds: int = 3, deploy: bool = True,
              max_test_rounds: int = 1) -> dict:
        if not self.s.agents.configured:
            raise ApiError(503, "ai_not_configured", "GEMINI_API_KEY is not set in .env; the pipeline cannot run")
        p = self.store.create(requirement, requested_by, {"max_fix_rounds": max_fix_rounds, "deploy": deploy,
                                                          "max_test_rounds": max_test_rounds})
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
        if p["outcome"] not in ("success", "built"):
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
        return PROPOSE if p["params"].get("deploy", True) else REPORT

    # ------------------------------------------------------------ deployment (a human decides) and tests
    def stage_propose(self, p: dict, log_fn) -> Optional[str]:
        try:
            job = self.s.deploys.propose_deploy(p["build_id"], f"pipeline:{p['id']}")
        except ApiError as e:
            raise StageFailed(f"build #{p['build_id']} is ready, but the deployment could not be proposed: "
                              f"{e.message}", outcome="not_deployed")
        self.store.update(p["id"], deploy_job_id=job["id"], stage=DEPLOYED, status=WAITING)
        self._event({**p, "stage": PROPOSE}, "waiting",
                    f"deployment of build #{p['build_id']} proposed as job #{job['id']}; waiting for a human "
                    f"(confirm text '{job['confirm_token']}')", {"job_id": job["id"]}, log_fn)
        return None

    def stage_deployed(self, p: dict, log_fn) -> Optional[str]:
        job = self.s.jobs.get(p["deploy_job_id"])
        if job is None:
            raise StageFailed(f"proposal job #{p['deploy_job_id']} has disappeared")
        if job["status"] == AWAITING:
            self.store.update(p["id"], status=WAITING)
            return None
        if job["status"] not in JOB_FINAL:
            job = self.s.jobs.wait(job["id"], 900)
        self.store.update(p["id"], status=RUNNING)
        if job["status"] == "SUCCEEDED":
            self._event(p, "done", f"build #{p['build_id']} is on the board (job #{job['id']}, approved by "
                                   f"{job.get('approved_by')})", log_fn=log_fn)
            return TEST
        if job["status"] in ("REJECTED", "EXPIRED", "CANCELLED"):
            raise StageFailed(f"the deployment was not made: job #{job['id']} {job['status']} "
                              f"({job.get('error') or ''})".rstrip(" ()"), outcome="not_deployed")
        raise StageFailed(f"the deployment failed: {job.get('error')}")

    def stage_test(self, p: dict, log_fn) -> str:
        cases = spec_cases(p["spec"])
        cases.insert(1, {"name": "runs the new build", "command": "VERSION",
                         "expect": {"contains": f"build={p['build_id']} "}})
        req = RunRequest(cases=[TestCase(**c) for c in cases[:50]], name=f"pipeline {p['id']} spec")
        job = self.s.jobs.wait(self.s.tests.start(req, f"pipeline:{p['id']}")["id"], 600)
        res = job.get("result") or {}
        self.store.update(p["id"], test_run_id=res.get("run_id"))
        tests = {"run_id": res.get("run_id"), "total": res.get("total"), "passed": res.get("passed"),
                 "failed": res.get("failed"), "aborted": res.get("aborted")}
        self.store.set_state(p["id"], tests=p["state"].get("tests", []) + [tests])
        if res.get("aborted") or not res:
            raise StageFailed(f"the tests could not run: {job.get('error')}")
        bad = [c for c in res["cases"] if not c["passed"]]
        if not bad:
            self._event(p, "done", f"all {res['total']} checks passed on the board", tests, log_fn)
            return REPORT
        self._event(p, "warning", f"{len(bad)} of {res['total']} checks failed on the board",
                    {"failed": [{k: c[k] for k in ("name", "command", "reply", "why")} for c in bad[:20]]}, log_fn)
        rounds, limit = p["test_rounds"] + 1, p["params"].get("max_test_rounds", 1)
        if rounds > limit or any(c["name"] in ("harness still answers", "runs the new build") for c in bad):
            raise StageFailed(f"{len(bad)} of {res['total']} checks failed on the board", outcome="tests_failed")
        self.store.update(p["id"], test_rounds=rounds)
        text = "\n".join(f"- {c['command']!r}: got {c['reply']!r}, {c['why']}" for c in bad[:20])
        self.store.set_state(p["id"], fix_task=f"Build #{p['build_id']} runs on the board, but these checks from "
                                               f"the specification FAILED. Fix the code so they pass:\n{text}")
        self._event(p, "fix", f"test round {rounds} of {limit}: back to the code", log_fn=log_fn)
        return CODE

    # ------------------------------------------------------------ resume / stop
    def resume(self, pid: int, who: str) -> dict:
        p = self.get(pid)
        if p["status"] != WAITING:
            raise ApiError(409, "pipeline_not_waiting", f"pipeline {pid} is {p['status']}, not waiting")
        job = self.s.jobs.get(p["deploy_job_id"]) if p["deploy_job_id"] else None
        if job is not None and job["status"] == AWAITING:
            raise ApiError(409, "still_awaiting_approval",
                           f"proposal job #{job['id']} still waits for a human; approve or reject it first")
        self.store.update(pid, status=QUEUED)
        job = self.s.jobs.submit("pipeline", {"pipeline_id": pid}, who, summary=f"resume pipeline {pid}")
        return {**self.get(pid), "job_id": job["id"]}

    def stop(self, pid: int, who: str) -> dict:
        p = self.get(pid)
        if p["status"] in FINAL:
            raise ApiError(409, "pipeline_finished", f"pipeline {pid} is already {p['status']}")
        self.store.update(pid, stop_requested=1)
        self.store.event(pid, p["stage"], "stop", f"stop requested by {who}")
        if p["status"] == WAITING:                      # nothing is running: finish it here
            self.store.update(pid, stage=REPORT, outcome="stopped", error=f"stopped by {who}")
            try:
                self.drive(pid, lambda t: None)
            except JobFailed:
                pass
        return self.get(pid)

    def stage_report(self, p: dict, log_fn) -> str:
        outcome = p["outcome"] or ("success" if p["params"].get("deploy", True) else "built")
        finished = now_iso()
        self._event(p, "done", f"pipeline finished: {outcome}", log_fn=log_fn)
        report = build_report({**p, "outcome": outcome}, self.s, finished)
        path = write_markdown(self.settings.root, p["id"], to_markdown(report, self.store.events(p["id"])))
        report["markdown_path"] = str(path)
        status = STOPPED if outcome == "stopped" else (FINISHED if outcome != "failed" else FAILED)
        self.store.update(p["id"], report_json=json.dumps(report, default=str), outcome=outcome, status=status,
                          finished_at=finished)
        return DONE

    def report_markdown(self, pid: int) -> str:
        p = self.get(pid)
        if not p["report"]:
            raise ApiError(409, "no_report_yet", f"pipeline {pid} has not finished")
        path = Path(p["report"].get("markdown_path") or "")
        return path.read_text(encoding="utf-8") if path.is_file() else to_markdown(p["report"], self.store.events(pid))
