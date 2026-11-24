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
from typing import Callable, Dict, Optional

from ..db import now_iso
from ..errors import ApiError
from ..fuzz.engine import Board, DEFAULT_BUDGET
from ..fuzz.repro import reproduce
from ..jobs import JobContext, JobFailed
from ..services.testing import RunRequest
from .store import (DONE, FAILED, FINAL, FINISHED, INSPECT, QUEUED, REPORT, REPRODUCE, RUNNING, STOPPED,
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
            rep = reproduce(board, {**f, "input_text": f["minimized"] or f["input_text"]}, 3)
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
        return REPORT

    # ------------------------------------------------------------ report
    def stage_report(self, r: dict, log_fn) -> str:
        outcome = r["outcome"] or "failed"
        status = STOPPED if outcome == "stopped" else (FINISHED if outcome not in ("failed",) else FAILED)
        report = {"outcome": outcome, "error": r["error"], "finding_id": r["finding_id"], "project": r["project"],
                  "before_build": r["before_build"], "after_build": r["after_build"]}
        self.store.update(r["id"], report_json=json.dumps(report, default=str), outcome=outcome, status=status,
                          finished_at=now_iso())
        self.event(r, "done", f"remediation finished: {outcome}", log_fn=log_fn)
        return DONE
