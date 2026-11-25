"""The remediation workflow: a plain state machine around one reproducible bug (a fuzz finding).

    reproduce  the bug must happen on the build that is on the board now (else there is nothing to fix)
    inspect    the build's sources, the minimised input, the scan; the workspace is put back to exactly that build;
               the project's suites run once as a BASELINE while the buggy build is still on the board
    ...        later stages: fix (root cause + patch + diff), build, scan, propose (a human approves), retest the
               original bug, regression against the baseline, accept - or roll back (a human approves again)
    report     everything above, from the records

Every decision that matters is made by plain code (reproduction counts, compiler, scanner, test runner); the model
only proposes the root cause and the patch.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from ..db import now_iso
from ..errors import ApiError
from ..fuzz.engine import Board, DEFAULT_BUDGET
from ..fuzz.repro import reproduce
from ..jobs import JobContext, JobFailed
from ..services.testing import RunRequest
from .fixer import code_diff, debug_task, root_cause_of
from .report import build_report, to_markdown, write_markdown
from ..build.engine import SUCCESS as BUILD_SUCCESS
from ..jobs import AWAITING
from ..jobs import FINAL as JOB_FINAL
from .store import (BUILD, DECIDE, DEPLOYED, DONE, FAILED, FINAL, FINISHED, FIX, INSPECT, PROPOSE, QUEUED,
                    REGRESSION, REPORT, REPRODUCE, RETEST, ROLLBACK, ROLLED_BACK, RUNNING, SCAN, STOPPED, WAITING,
                    RemediationStore)

log = logging.getLogger("aeisp.remediate")


class StageFailed(Exception):
    def __init__(self, message: str, outcome: str = "failed") -> None:
        super().__init__(message)
        self.message, self.outcome = message, outcome


class RemediationEngine:
    def __init__(self, services, settings) -> None:
        self.s = services
        self.settings = settings
        self.store = RemediationStore(settings.db_path)
        self.recover_s: Optional[float] = None
        self.sleep = None
        self.handlers: Dict[str, Callable] = {REPRODUCE: self.stage_reproduce, INSPECT: self.stage_inspect,
                                              FIX: self.stage_fix, BUILD: self.stage_build, SCAN: self.stage_scan,
                                              PROPOSE: self.stage_propose, DEPLOYED: self.stage_deployed,
                                              RETEST: self.stage_retest, REGRESSION: self.stage_regression,
                                              DECIDE: self.stage_decide, ROLLBACK: self.stage_rollback,
                                              ROLLED_BACK: self.stage_rolled_back,
                                              REPORT: self.stage_report}
        services.jobs.register("remediation", self._run_job, "ai")

    def recover(self) -> int:
        return self.store.recover_orphans()

    # ------------------------------------------------------------ starting
    def start(self, finding_id: int, who: str, max_fix_rounds: int = 2, fuzz_check: bool = True) -> dict:
        if not self.s.agents.configured:
            raise ApiError(503, "ai_not_configured", "GEMINI_API_KEY is not set in .env; remediation cannot run")
        f = self.s.fuzz.finding(finding_id)
        if f["status"] in ("fixed", "wont_fix"):
            raise ApiError(409, "finding_closed", f"finding #{finding_id} is already {f['status']}")
        if f["severity"] == "info":
            raise ApiError(422, "not_a_bug", f"finding #{finding_id} is informational; there is nothing to fix")
        active = self.store.active_for(finding_id)
        if active:
            raise ApiError(409, "already_running", f"remediation {active['id']} already works on finding #{finding_id}")
        r = self.store.create(finding_id, f.get("project"), who,
                              {"max_fix_rounds": max_fix_rounds, "fuzz_check": fuzz_check})
        job = self.s.jobs.submit("remediation", {"remediation_id": r["id"]}, who,
                                 summary=f"remediate finding #{finding_id} ({f['kind']}: {f['input_text'][:40]!r})")
        return {**self.store.get(r["id"]), "job_id": job["id"]}

    def get(self, rid: int) -> dict:
        return self.store.get(rid)

    def _pending_job(self, r: dict) -> Optional[int]:
        return r["rollback_job_id"] if r["stage"] in ("rolled_back",) else r["deploy_job_id"]

    def resume(self, rid: int, who: str) -> dict:
        r = self.get(rid)
        if r["status"] != WAITING:
            raise ApiError(409, "not_waiting", f"remediation {rid} is {r['status']}, not waiting")
        jid = self._pending_job(r)
        job = self.s.jobs.get(jid) if jid else None
        if job is not None and job["status"] == AWAITING:
            raise ApiError(409, "still_awaiting_approval",
                           f"proposal job #{job['id']} still waits for a human; approve or reject it first")
        self.store.update(rid, status=QUEUED)
        job = self.s.jobs.submit("remediation", {"remediation_id": rid}, who, summary=f"resume remediation {rid}")
        return {**self.get(rid), "job_id": job["id"]}

    def stop(self, rid: int, who: str) -> dict:
        r = self.get(rid)
        if r["status"] in FINAL:
            raise ApiError(409, "remediation_finished", f"remediation {rid} is already {r['status']}")
        self.store.update(rid, stop_requested=1)
        if r["status"] == WAITING:
            self.store.update(rid, stage=REPORT, outcome="stopped", error=f"stopped by {who}")
            try:
                self.drive(rid, lambda t: None)
            except JobFailed:
                pass
        return self.get(rid)

    # ------------------------------------------------------------ the loop
    def _run_job(self, ctx: JobContext) -> dict:
        return self.drive(ctx.params["remediation_id"], ctx.log)

    def drive(self, rid: int, log_fn: Callable[[str], None]) -> dict:
        self.store.update(rid, status=RUNNING)
        while True:
            r = self.get(rid)
            stage = r["stage"]
            if stage == DONE:
                break
            if r["stop_requested"] and stage != REPORT:
                self.event(r, "stop", "stopped by request")
                self.store.update(rid, stage=REPORT, outcome="stopped", error="stopped by request")
                continue
            try:
                nxt = self.handlers[stage](r, log_fn)
            except StageFailed as e:
                self.event(r, "failed", e.message, log_fn=log_fn)
                self.store.update(rid, stage=REPORT, outcome=e.outcome, error=e.message)
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("remediation %s crashed in %s", rid, stage)
                self.event(r, "crash", f"{type(e).__name__}: {e}")
                self.store.update(rid, stage=REPORT, outcome="failed", error=f"{stage}: {type(e).__name__}: {e}")
                continue
            if nxt is None:                                   # paused for a human
                return {"remediation_id": rid, "status": self.get(rid)["status"], "stage": self.get(rid)["stage"]}
            self.store.update(rid, stage=nxt)
        r = self.get(rid)
        out = {"remediation_id": rid, "status": r["status"], "outcome": r["outcome"], "after_build": r["after_build"]}
        if r["outcome"] != "fixed":
            raise JobFailed(f"remediation {rid} ended: {r['outcome']} - {r['error'] or ''}".strip(" -"), out)
        return out

    def event(self, r: dict, kind: str, message: str, detail: Optional[dict] = None, log_fn=None) -> None:
        self.store.event(r["id"], r["stage"], kind, message, detail)
        if log_fn:
            log_fn(f"[{r['stage']}] {message}")

    def _board(self, send) -> Board:
        return Board(send, self.recover_s or DEFAULT_BUDGET["recover_s"], self.sleep or time.sleep)

    # ------------------------------------------------------------ reproduce
    def stage_reproduce(self, r: dict, log_fn) -> str:
        f = self.s.fuzz.finding(r["finding_id"])
        with self.s.device.session(f"remediation #{r['id']}") as send:
            board = self._board(send)
            if not board.recover():
                raise StageFailed("the board does not answer PING", "no_board")
            fw = board.firmware()
            rep = reproduce(board, {**f, "input_text": f["minimized"] or f["input_text"]}, 3,
                            self.s.fuzz.rules_for(f.get("project")))
            board.recover()
        self.store.set_state(r["id"], reproduction=rep, board_before=fw)
        if not fw.get("build_id"):
            raise StageFailed("the board does not report which build it runs (VERSION)")
        self.store.update(r["id"], before_build=fw["build_id"])
        if rep["hits"] == 0:
            raise StageFailed(f"the bug does not happen on build #{fw['build_id']} (0 of {rep['attempts']} tries); "
                              "there is nothing to fix", "not_reproduced")
        self.event(r, "done", f"the bug happens on build #{fw['build_id']}: {rep['hits']} of {rep['attempts']} tries",
                   {"tries": rep["tries"]}, log_fn)
        return INSPECT

    # ------------------------------------------------------------ inspect
    def _project_suites(self, project: Optional[str]) -> list:
        names = [s["name"] for s in self.s.tests.suites_db.list(project)] if project else []
        return names + ["smoke"]

    def run_suites(self, names: list, who: str) -> Dict[str, dict]:
        out = {}
        for name in names:
            job = self.s.jobs.wait(self.s.tests.start(RunRequest(suite=name), who)["id"], 600)
            res = job.get("result") or {}
            out[name] = {"run_id": res.get("run_id"), "passed": res.get("passed"), "failed": res.get("failed"),
                         "total": res.get("total"), "build_id": (res.get("firmware") or {}).get("build_id")}
        return out

    def stage_inspect(self, r: dict, log_fn) -> str:
        f = self.s.fuzz.finding(r["finding_id"])
        try:
            build = self.s.builds.get(r["before_build"])
        except ApiError:
            raise StageFailed(f"build #{r['before_build']} (on the board) is not in the build history; the sources "
                              "that produced the bug are unknown")
        project = build["project"]
        sources = self.s.analysis.build_sources(r["before_build"])
        current = self.s.analysis.project_sources(project)
        if current != {k: v for k, v in sources.items() if k.endswith((".c", ".h"))}:
            self.s.firmware.restore_sources(project, r["before_build"])
            self.event(r, "info", f"the workspace of {project} differed from build #{r['before_build']}; it was put "
                                  "back to that build so the fix starts from the code that has the bug",
                       log_fn=log_fn)
        scan = self.s.analysis.security_scan(build_id=r["before_build"])
        baseline = self.run_suites(self._project_suites(project), f"remediation:{r['id']}")
        evidence = {
            "finding": {k: f[k] for k in ("id", "kind", "severity", "cwe", "detail", "input_text", "minimized",
                                          "reply", "input_family", "input_note")},
            "recipe": (f["reproductions"][-1]["recipe"] if f["reproductions"] else None),
            "files": sorted(sources), "scan": {"verdict": scan["verdict"], "findings": scan["findings"][:10]},
        }
        self.store.update(r["id"], project=project)
        self.store.set_state(r["id"], evidence=evidence, baseline=baseline)
        self.event(r, "done", f"project {project}, {len(sources)} files, scan {scan['verdict']}, baseline: "
                              + ", ".join(f"{n} {b['passed']}/{b['total']}" for n, b in baseline.items()),
                   {"baseline": baseline}, log_fn)
        return self.after_inspect(r)

    def after_inspect(self, r: dict) -> str:
        return FIX

    # ------------------------------------------------------------ fix: root cause, patch, diff
    def stage_fix(self, r: dict, log_fn) -> str:
        state = r["state"]
        task = debug_task(state["evidence"], r["project"], r["before_build"], state.get("retry_note"))
        res = self.s.agents.run_inline("debugger", task, f"remediation:{r['id']}", {"remediation_id": r["id"]}, log_fn,
                                       project=r["project"])
        runs = state.get("agent_runs", []) + [{"run_id": res["child_run_id"], "outcome": res["outcome"],
                                               "tokens": res.get("tokens", 0)}]
        self.store.set_state(r["id"], agent_runs=runs, retry_note=None)
        if res["status"] == "FAILED" or (res.get("error") or "").startswith("the model failed"):
            raise StageFailed(f"the debugger agent could not work: {(res.get('error') or '')[:300]}")
        before = self.s.analysis.build_sources(r["before_build"])
        after = self.s.analysis.project_sources(r["project"])
        diff = code_diff({k: v for k, v in before.items() if k.endswith((".c", ".h"))}, after)
        cause = root_cause_of(res)
        self.store.update(r["id"], diff_text=diff["diff"], root_cause=cause)
        self.store.set_state(r["id"], diff_stats={k: diff[k] for k in ("files", "added", "removed")},
                             debugger_report=res.get("report"))
        if not diff["files"]:
            raise StageFailed("the debugger agent did not change any code", "no_fix")
        self.event(r, "done", f"debugger run {res['child_run_id']}: {', '.join(diff['files'])} changed "
                              f"(+{diff['added']} -{diff['removed']}); root cause: {cause[:160]}",
                   {"root_cause": cause, "diff": diff["diff"][:4000]}, log_fn)
        return self.after_fix(r)

    def after_fix(self, r: dict) -> str:
        return BUILD

    def _fix_round(self, r: dict, why: str, note: str, log_fn) -> str:
        rounds, limit = r["fix_rounds"] + 1, r["params"].get("max_fix_rounds", 2)
        if rounds > limit:
            raise StageFailed(f"{why}, and {limit} fix rounds did not solve it", "fix_failed")
        self.store.update(r["id"], fix_rounds=rounds)
        self.store.set_state(r["id"], retry_note=note)
        self.event(r, "fix", f"{why}: back to the debugger (round {rounds} of {limit})", log_fn=log_fn)
        return FIX

    # ------------------------------------------------------------ build, scan
    def stage_build(self, r: dict, log_fn) -> str:
        job = self.s.jobs.wait(self.s.builds.start(r["project"], f"remediation:{r['id']}")["id"], 1200)
        res = job.get("result") or {}
        builds = r["state"].get("builds", []) + [{"build_id": res.get("id"), "status": res.get("status"),
                                                  "errors": res.get("errors")}]
        self.store.set_state(r["id"], builds=builds)
        if res.get("status") == BUILD_SUCCESS:
            self.store.update(r["id"], after_build=res["id"])
            self.event(r, "done", f"build #{res['id']} SUCCESS", {"build_id": res["id"]}, log_fn)
            return SCAN
        if res.get("status") != "FAILED":
            raise StageFailed(f"the build could not run: {job.get('error')}")
        errors = [d for d in res.get("diagnostics", []) if d.get("severity") == "error"][:15]
        text = "\n".join(f"- {d.get('file')}:{d.get('line')}: {d.get('message')}" for d in errors)
        return self._fix_round(r, f"build #{res.get('id')} failed", f"The fixed code does not compile:\n{text}", log_fn)

    def stage_scan(self, r: dict, log_fn) -> str:
        scan = self.s.analysis.security_scan(build_id=r["after_build"])
        self.store.set_state(r["id"], scan_after={"verdict": scan["verdict"], "counts": scan["counts"]})
        if scan["verdict"] == "PASS":
            self.event(r, "done", f"security scan of build #{r['after_build']}: PASS", log_fn=log_fn)
            return PROPOSE
        bad = [f for f in scan["findings"] if f["severity"] in ("critical", "high")][:10]
        text = "\n".join(f"- {f['file']}:{f['line']} [{f['rule']}] {f['title']} -> {f['advice']}" for f in bad)
        return self._fix_round(r, f"build #{r['after_build']} fails the security scan",
                               f"The fix introduced security problems:\n{text}", log_fn)

    # ------------------------------------------------------------ deployment: a human decides
    def stage_propose(self, r: dict, log_fn) -> Optional[str]:
        try:
            job = self.s.deploys.propose_deploy(r["after_build"], f"remediation:{r['id']}")
        except ApiError as e:
            raise StageFailed(f"build #{r['after_build']} has the fix, but its deployment could not be proposed: "
                              f"{e.message}", "not_deployed")
        self.store.update(r["id"], deploy_job_id=job["id"], stage=DEPLOYED, status=WAITING)
        self.event({**r, "stage": PROPOSE}, "waiting", f"deployment of the fixed build #{r['after_build']} proposed "
                   f"as job #{job['id']}; waiting for a human (confirm text '{job['confirm_token']}')",
                   {"job_id": job["id"]}, log_fn)
        return None

    def _job_decided(self, r: dict, job_id: int) -> Optional[dict]:
        job = self.s.jobs.get(job_id)
        if job is None:
            raise StageFailed(f"proposal job #{job_id} has disappeared")
        if job["status"] == AWAITING:
            self.store.update(r["id"], status=WAITING)
            return None
        if job["status"] not in JOB_FINAL:
            job = self.s.jobs.wait(job_id, 900)
        self.store.update(r["id"], status=RUNNING)
        return job

    def stage_deployed(self, r: dict, log_fn) -> Optional[str]:
        job = self._job_decided(r, r["deploy_job_id"])
        if job is None:
            return None
        if job["status"] == "SUCCEEDED":
            self.event(r, "done", f"build #{r['after_build']} is on the board (approved by {job.get('approved_by')})",
                       log_fn=log_fn)
            return RETEST
        if job["status"] in ("REJECTED", "EXPIRED", "CANCELLED"):
            raise StageFailed(f"the fix was not deployed: job #{job['id']} {job['status']}", "not_deployed")
        raise StageFailed(f"the deployment of the fix failed (the deploy engine rolled back): {job.get('error')}",
                          "deploy_failed")

    # ------------------------------------------------------------ the original bug, again
    def stage_retest(self, r: dict, log_fn) -> str:
        f = self.s.fuzz.finding(r["finding_id"])
        inputs = [f["minimized"] or f["input_text"]] + ([f["input_text"]] if f["minimized"] else [])
        results = []
        with self.s.device.session(f"remediation #{r['id']} retest") as send:
            board = self._board(send)
            if not board.recover():
                raise StageFailed("the board does not answer PING after the deployment")
            fw = board.firmware()
            if fw.get("build_id") != r["after_build"]:
                raise StageFailed(f"the board runs build #{fw.get('build_id')}, not the fix #{r['after_build']}")
            for text in dict.fromkeys(inputs):
                rep = reproduce(board, {**f, "input_text": text}, 5 if text == inputs[0] else 2,
                                self.s.fuzz.rules_for(f.get("project")))
                results.append({"input": text, "hits": rep["hits"], "attempts": rep["attempts"]})
            board.recover()
        self.store.set_state(r["id"], retest=results)
        hits = sum(x["hits"] for x in results)
        if hits:
            note = "; ".join(f"{x['input']!r} still fails {x['hits']} of {x['attempts']} times" for x in results)
            return self._fix_round(r, f"the bug still happens on build #{r['after_build']}",
                                   f"On the board, with your fix (build #{r['after_build']}): {note}.", log_fn)
        self.event(r, "done", "the original bug is gone: " + ", ".join(f"{x['input']!r} 0/{x['attempts']}"
                                                                        for x in results), {"retest": results}, log_fn)
        return self.after_retest(r)

    def after_retest(self, r: dict) -> str:
        return REGRESSION

    # ------------------------------------------------------------ nothing else may break
    QUICK_FUZZ = {"families": ["boundary", "invalid", "overflow", "oversize"], "max_inputs": 150, "max_resets": 2}

    def stage_regression(self, r: dict, log_fn) -> str:
        baseline = r["state"].get("baseline", {})
        after = self.run_suites(list(baseline), f"remediation:{r['id']}")
        regressions, compared = [], {}
        for name, base in baseline.items():
            new = after.get(name, {})
            if not (base.get("run_id") and new.get("run_id")):
                continue
            cmp = self.s.tests.compare(base["run_id"], new["run_id"])
            compared[name] = {"verdict": cmp["verdict"], "counts": cmp["counts"],
                              "regressed": [x["name"] for x in cmp["regressed"]]}
            regressions += [{"suite": name, "case": x["name"], "before": x["before"], "after": x["after"]}
                            for x in cmp["regressed"]]
        fuzz = None
        if r["params"].get("fuzz_check", True):
            f = self.s.fuzz.finding(r["finding_id"])
            camp = self.s.fuzz.start(f"remediation:{r['id']}", r["project"], self.QUICK_FUZZ["families"], seed=1,
                                     max_inputs=self.QUICK_FUZZ["max_inputs"],
                                     max_resets=self.QUICK_FUZZ["max_resets"])
            self.s.jobs.wait(camp["job_id"], 900)
            found = self.s.fuzz.store.findings(campaign_id=camp["id"], min_severity="medium")
            fuzz = {"campaign_id": camp["id"], "serious": [{k: x[k] for k in ("id", "kind", "severity", "input_text")}
                                                         for x in found]}
        self.store.set_state(r["id"], after_suites=after, regression=compared, regressions=regressions,
                             fuzz_check=fuzz)
        self.event(r, "done" if not regressions else "warning",
                   f"{len(regressions)} regressions in {len(compared)} suites"
                   + (f", quick fuzz: {len(fuzz['serious'])} serious findings" if fuzz else ""),
                   {"regressions": regressions, "fuzz": fuzz}, log_fn)
        return DECIDE

    # ------------------------------------------------------------ accept, or reject and roll back
    def stage_decide(self, r: dict, log_fn) -> str:
        st = r["state"]
        reasons = [f"{x['suite']}/{x['case']}: {x['before']!r} -> {x['after']!r}" for x in st.get("regressions", [])]
        reasons += [f"fuzz finding #{x['id']} {x['kind']} on {x['input_text'][:40]!r}"
                    for x in (st.get("fuzz_check") or {}).get("serious", [])]
        if not reasons:
            self.s.fuzz.set_status(r["finding_id"], "fixed")
            self.store.update(r["id"], outcome="fixed")
            self.event(r, "done", f"fix ACCEPTED: the bug is gone on build #{r['after_build']} and nothing regressed; "
                                  f"finding #{r['finding_id']} is marked fixed", log_fn=log_fn)
            return REPORT
        self.store.set_state(r["id"], rejection=reasons)
        self.event(r, "warning", "fix REJECTED: " + "; ".join(reasons[:5]), {"reasons": reasons}, log_fn)
        return ROLLBACK

    def stage_rollback(self, r: dict, log_fn) -> Optional[str]:
        self.s.firmware.restore_sources(r["project"], r["before_build"])
        try:
            job = self.s.deploys.propose_deploy(r["before_build"], f"remediation:{r['id']} rollback")
        except ApiError as e:
            self.store.update(r["id"], outcome="rejected_not_rolled_back",
                              error=f"the rollback to build #{r['before_build']} could not be proposed: {e.message}")
            return REPORT
        self.store.update(r["id"], rollback_job_id=job["id"], stage=ROLLED_BACK, status=WAITING)
        self.event({**r, "stage": ROLLBACK}, "waiting",
                   f"sources restored to build #{r['before_build']}; its redeployment is proposed as job #{job['id']} "
                   f"(confirm text '{job['confirm_token']}')", {"job_id": job["id"]}, log_fn)
        return None

    def stage_rolled_back(self, r: dict, log_fn) -> Optional[str]:
        job = self._job_decided(r, r["rollback_job_id"])
        if job is None:
            return None
        if job["status"] == "SUCCEEDED":
            self.store.update(r["id"], outcome="rejected_rolled_back")
            self.event(r, "done", f"rolled back: the board runs build #{r['before_build']} again", log_fn=log_fn)
        else:
            self.store.update(r["id"], outcome="rejected_not_rolled_back",
                              error=f"the rollback was not made (job #{job['id']} {job['status']}); the board still "
                                    f"runs the rejected build #{r['after_build']}")
        return REPORT

    # ------------------------------------------------------------ report
    def stage_report(self, r: dict, log_fn) -> str:
        outcome = r["outcome"] or "failed"
        status = STOPPED if outcome == "stopped" else (FINISHED if outcome not in ("failed",) else FAILED)
        self.event(r, "done", f"remediation finished: {outcome}", log_fn=log_fn)
        report = build_report({**r, "outcome": outcome}, self.s.fuzz.finding(r["finding_id"]), self.s)
        path = write_markdown(self.settings.root, r["id"],
                              to_markdown(report, r.get("diff_text") or "", self.store.events(r["id"])))
        report["markdown_path"] = str(path)
        self.store.update(r["id"], report_json=json.dumps(report, default=str), outcome=outcome, status=status,
                          finished_at=now_iso())
        return DONE

    def report_markdown(self, rid: int) -> str:
        r = self.get(rid)
        if not r["report"]:
            raise ApiError(409, "no_report_yet", f"remediation {rid} has not finished")
        path = Path(r["report"].get("markdown_path") or "")
        return path.read_text(encoding="utf-8") if path.is_file() else \
            to_markdown(r["report"], r.get("diff_text") or "", self.store.events(rid))
