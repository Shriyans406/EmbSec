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
from ..pipeline.spec import spec_for_coder
from ..remediate.fixer import code_diff
from ..build.engine import SUCCESS as BUILD_SUCCESS
from ..jobs import AWAITING
from ..jobs import FINAL as JOB_FINAL
from .store import (ANALYSE, BASELINE, BUILD, CHECK, DECIDE, DEPLOYED, DONE, FAILED, FINAL, FINISHED, GOALS,
                    MEASURE, PROPOSE, QUEUED, REPORT, ROLLBACK, ROLLED_BACK, RUNNING, SCAN, STOPPED, VARIANT,
                    WAITING, OptimizationStore)

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
        self.handlers: Dict[str, Callable] = {BASELINE: self.stage_baseline, ANALYSE: self.stage_analyse,
                                              VARIANT: self.stage_variant, BUILD: self.stage_build,
                                              SCAN: self.stage_scan, PROPOSE: self.stage_propose,
                                              DEPLOYED: self.stage_deployed, MEASURE: self.stage_measure,
                                              CHECK: self.stage_check, DECIDE: self.stage_decide,
                                              ROLLBACK: self.stage_rollback, ROLLED_BACK: self.stage_rolled_back,
                                              REPORT: self.stage_report}
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

    def resume(self, oid: int, who: str) -> dict:
        o = self.get(oid)
        if o["status"] != WAITING:
            raise ApiError(409, "not_waiting", f"optimisation {oid} is {o['status']}, not waiting")
        jid = o["rollback_job_id"] if o["stage"] == "rolled_back" else o["deploy_job_id"]
        job = self.s.jobs.get(jid) if jid else None
        if job is not None and job["status"] == AWAITING:
            raise ApiError(409, "still_awaiting_approval",
                           f"proposal job #{job['id']} still waits for a human; approve or reject it first")
        self.store.update(oid, status=QUEUED)
        job = self.s.jobs.submit("optimization", {"optimization_id": oid}, who, summary=f"resume optimisation {oid}")
        return {**self.get(oid), "job_id": job["id"]}

    def stop(self, oid: int, who: str) -> dict:
        o = self.get(oid)
        if o["status"] in FINAL:
            raise ApiError(409, "optimization_finished", f"optimisation {oid} is already {o['status']}")
        self.store.update(oid, stop_requested=1)
        if o["status"] == WAITING:
            self.store.update(oid, stage=REPORT, outcome="stopped", error=f"stopped by {who}")
            try:
                self.drive(oid, lambda t: None)
            except JobFailed:
                pass
        return self.get(oid)

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
        return ANALYSE

    # ------------------------------------------------------------ analyse: the Performance agent
    def target_of(self, o: dict, bench: dict) -> Optional[str]:
        if o["goal"] != "speed":
            return None
        cmds = bench["result"]["commands"]
        if o["target"]:
            if o["target"] not in cmds:
                raise StageFailed(f"the benchmark has no command {o['target']!r} (it has {sorted(cmds)})")
            return o["target"]
        timed = {n: (c["device_us"] or c["host_ms"])["median"] for n, c in cmds.items() if n != "ping"}
        return max(timed, key=timed.get) if timed else None

    def _numbers(self, bench: dict, size: dict) -> str:
        lines = [f"flash {size.get('flash_bytes')} bytes, RAM {size.get('ram_bytes')} bytes"]
        for n, c in bench["result"]["commands"].items():
            if c.get("device_us"):
                lines.append(f"{n} ({c['command']}): {c['device_us']['median']} us on the device "
                             f"(stdev {c['device_us']['stdev']}), host round trip {c['host_ms']['median']} ms")
            else:
                lines.append(f"{n} ({c['command']}): host round trip {c['host_ms']['median']} ms (no device timing)")
        return "\n".join(lines)

    def stage_analyse(self, o: dict, log_fn) -> str:
        bench = self.s.profiling.run(o["base_run_id"])
        target = self.target_of(o, bench)
        st = o["state"]
        goal = {"speed": f"make {target!r} ({bench['result']['commands'][target]['command']}) faster on the device"
                if target else "make the app commands faster", "flash": "use less flash", "ram": "use less RAM"}[o["goal"]]
        task = [f"Project '{o['project']}', build #{o['base_build']} (on the board). Goal: {goal}, by at least "
                f"{o['min_gain_pct']:g} %, without changing any reply.", "", "Measured baseline:",
                self._numbers(bench, st["base_size"])]
        if st.get("tried"):
            task += ["", "Variants already tried and REJECTED (do not propose them again):"]
            task += [f"- {x['proposal']}: {x['why']}" for x in st["tried"]]
        task += ["", "Read the sources and the size report of the build. Propose exactly ONE alternative "
                     "implementation that reaches the goal: say precisely what to change (function, lines) and the "
                     "effect you expect. Put it first in 'improvements'."]
        res = self.s.agents.run_inline("performance", "\n".join(task), f"optimisation:{o['id']}",
                                       {"optimization_id": o["id"]}, log_fn)
        runs = st.get("agent_runs", []) + [{"run_id": res["child_run_id"], "agent": "performance",
                                            "outcome": res["outcome"], "tokens": res.get("tokens", 0)}]
        self.store.set_state(o["id"], agent_runs=runs, target=target)
        if res["status"] == "FAILED" or (res.get("error") or "").startswith("the model failed"):
            raise StageFailed(f"the performance agent could not work: {(res.get('error') or '')[:300]}")
        ideas = (res.get("report") or {}).get("improvements") or []
        if not ideas:
            raise StageFailed("the performance agent found nothing to improve", "no_idea")
        self.store.set_state(o["id"], proposal=ideas[0], analysis=(res.get("report") or {}).get("summary"))
        self.event(o, "done", f"performance agent run {res['child_run_id']}: {ideas[0][:200]}",
                   {"improvements": ideas[:5]}, log_fn)
        return VARIANT

    # ------------------------------------------------------------ variant: the Firmware agent
    def stage_variant(self, o: dict, log_fn) -> str:
        st = o["state"]
        spec = self.s.tests.latest_spec(o["project"])
        task = [f"Optimise project '{o['project']}' (it is build #{o['base_build']}). Make exactly this change:",
                st["proposal"], "",
                "Every reply must stay byte-for-byte the same for every input (the same checks run before and "
                "after), and the code must stay safe (the security scan runs again). Change nothing else."]
        if st.get("retry_note"):
            task += ["", "YOUR PREVIOUS ATTEMPT:", st["retry_note"]]
        if spec:
            task += ["", "--- the specification the replies must still meet ---", spec_for_coder(spec)]
        res = self.s.agents.run_inline("firmware", "\n".join(task), f"optimisation:{o['id']}",
                                       {"optimization_id": o["id"]}, log_fn)
        runs = st.get("agent_runs", []) + [{"run_id": res["child_run_id"], "agent": "firmware",
                                            "outcome": res["outcome"], "tokens": res.get("tokens", 0)}]
        self.store.set_state(o["id"], agent_runs=runs, retry_note=None)
        if res["status"] == "FAILED" or (res.get("error") or "").startswith("the model failed"):
            raise StageFailed(f"the firmware agent could not work: {(res.get('error') or '')[:300]}")
        before = {k: v for k, v in self.s.analysis.build_sources(o["base_build"]).items() if k.endswith((".c", ".h"))}
        diff = code_diff(before, self.s.analysis.project_sources(o["project"]))
        self.store.update(o["id"], diff_text=diff["diff"], variants_tried=o["variants_tried"] + 1)
        self.store.set_state(o["id"], diff_stats={k: diff[k] for k in ("files", "added", "removed")})
        if not diff["files"]:
            raise StageFailed("the firmware agent did not change any code", "no_change")
        self.event(o, "done", f"variant {o['variants_tried'] + 1}: {', '.join(diff['files'])} changed "
                              f"(+{diff['added']} -{diff['removed']})", {"diff": diff["diff"][:4000]}, log_fn)
        return self.after_variant(o)

    def after_variant(self, o: dict) -> str:
        return BUILD

    # ------------------------------------------------------------ a variant that is not good enough
    def reject_variant(self, o: dict, why: str, log_fn, deployed: bool = False) -> str:
        st = o["state"]
        tried = st.get("tried", []) + [{"variant": o["variants_tried"], "proposal": st.get("proposal"),
                                        "build": o["variant_build"], "why": why}]
        self.store.set_state(o["id"], tried=tried)
        self.event(o, "warning", f"variant {o['variants_tried']} REJECTED: {why}", log_fn=log_fn)
        self.s.firmware.restore_sources(o["project"], o["base_build"])
        if deployed:
            return ROLLBACK                                  # the board runs the variant: put the baseline back
        return self.next_variant(o)

    def next_variant(self, o: dict) -> str:
        o = self.get(o["id"])
        if o["variants_tried"] < o["params"].get("max_variants", 2):
            self.store.update(o["id"], variant_build=None, fix_rounds=0)
            return ANALYSE
        self.store.update(o["id"], outcome="no_gain")
        return REPORT

    # ------------------------------------------------------------ build, scan, size gate
    def stage_build(self, o: dict, log_fn) -> str:
        job = self.s.jobs.wait(self.s.builds.start(o["project"], f"optimisation:{o['id']}")["id"], 1200)
        res = job.get("result") or {}
        builds = o["state"].get("builds", []) + [{"build_id": res.get("id"), "status": res.get("status")}]
        self.store.set_state(o["id"], builds=builds)
        if res.get("status") == BUILD_SUCCESS:
            self.store.update(o["id"], variant_build=res["id"])
            self.event(o, "done", f"build #{res['id']} SUCCESS", log_fn=log_fn)
            return SCAN
        if res.get("status") != "FAILED":
            raise StageFailed(f"the build could not run: {job.get('error')}")
        errors = [d for d in res.get("diagnostics", []) if d.get("severity") == "error"][:15]
        text = "\n".join(f"- {d.get('file')}:{d.get('line')}: {d.get('message')}" for d in errors)
        if o["fix_rounds"] < 1:
            self.store.update(o["id"], fix_rounds=o["fix_rounds"] + 1)
            self.store.set_state(o["id"], retry_note=f"It did not compile:\n{text}")
            return VARIANT
        return self.reject_variant(o, f"build #{res.get('id')} does not compile", log_fn)

    def stage_scan(self, o: dict, log_fn) -> str:
        scan = self.s.analysis.security_scan(build_id=o["variant_build"])
        if scan["verdict"] != "PASS":
            bad = [f"{f['rule']} {f['title']} ({f['file']}:{f['line']})" for f in scan["findings"]
                   if f["severity"] in ("critical", "high")][:5]
            return self.reject_variant(o, "the security scan fails: " + "; ".join(bad), log_fn)
        base, new = o["state"]["base_size"], self.size_of(o["variant_build"])
        size = {k: {"before": base.get(k), "after": new.get(k),
                    "change_pct": round(100 * (new[k] - base[k]) / base[k], 2) if base.get(k) else None}
                for k in ("flash_bytes", "ram_bytes") if base.get(k) is not None and new.get(k) is not None}
        self.store.set_state(o["id"], size=size)
        if o["goal"] in ("flash", "ram"):
            key = f"{o['goal']}_bytes"
            change = (size.get(key) or {}).get("change_pct")
            if change is None or -change < o["min_gain_pct"]:
                return self.reject_variant(o, f"{o['goal']} changed by {change} %, the goal is at least "
                                              f"-{o['min_gain_pct']:g} %", log_fn)
        elif (size.get("flash_bytes") or {}).get("change_pct", 0) > 10:
            return self.reject_variant(o, f"flash grows by {size['flash_bytes']['change_pct']} % (more than 10 %)",
                                       log_fn)
        self.event(o, "done", f"security scan PASS; size {size}", {"size": size}, log_fn)
        return PROPOSE

    # ------------------------------------------------------------ deployment: a human decides
    def stage_propose(self, o: dict, log_fn) -> Optional[str]:
        try:
            job = self.s.deploys.propose_deploy(o["variant_build"], f"optimisation:{o['id']}")
        except ApiError as e:
            self.s.firmware.restore_sources(o["project"], o["base_build"])
            raise StageFailed(f"the variant build #{o['variant_build']} could not be proposed: {e.message}",
                              "not_deployed")
        self.store.update(o["id"], deploy_job_id=job["id"], stage=DEPLOYED, status=WAITING)
        self.event({**o, "stage": PROPOSE}, "waiting", f"deployment of variant build #{o['variant_build']} proposed "
                   f"as job #{job['id']}; waiting for a human (confirm text '{job['confirm_token']}')",
                   {"job_id": job["id"]}, log_fn)
        return None

    def _job_decided(self, o: dict, job_id: int) -> Optional[dict]:
        job = self.s.jobs.get(job_id)
        if job is None:
            raise StageFailed(f"proposal job #{job_id} has disappeared")
        if job["status"] == AWAITING:
            self.store.update(o["id"], status=WAITING)
            return None
        if job["status"] not in JOB_FINAL:
            job = self.s.jobs.wait(job_id, 900)
        self.store.update(o["id"], status=RUNNING)
        return job

    def stage_deployed(self, o: dict, log_fn) -> Optional[str]:
        job = self._job_decided(o, o["deploy_job_id"])
        if job is None:
            return None
        if job["status"] == "SUCCEEDED":
            self.event(o, "done", f"variant build #{o['variant_build']} is on the board", log_fn=log_fn)
            return MEASURE
        self.s.firmware.restore_sources(o["project"], o["base_build"])
        if job["status"] in ("REJECTED", "EXPIRED", "CANCELLED"):
            raise StageFailed(f"the variant was not deployed: job #{job['id']} {job['status']}", "not_deployed")
        raise StageFailed(f"the deployment of the variant failed (the deploy engine rolled back): {job.get('error')}",
                          "deploy_failed")

    # ------------------------------------------------------------ A/B: the variant against the baseline
    def stage_measure(self, o: dict, log_fn) -> str:
        if self.board_build()["build_id"] != o["variant_build"]:
            raise StageFailed(f"the board does not run the variant #{o['variant_build']}")
        run = self.run_bench(o["benchmark"], f"optimisation:{o['id']}")
        self.store.update(o["id"], variant_run_id=run["id"])
        cmp = self.s.profiling.compare(o["base_run_id"], run["id"])
        self.store.set_state(o["id"], comparison=cmp)
        target = o["state"].get("target")
        problems = []
        if o["goal"] == "speed":
            t = (cmp["commands"].get(target) or {})
            m = t.get("device_us") or t.get("host_ms")
            if not m or m["verdict"] != "better" or (m["change_pct"] or 0) > -o["min_gain_pct"]:
                problems.append(f"{target}: {m and m['before']} -> {m and m['after']} "
                                f"({m and m['change_pct']} %), needs at least -{o['min_gain_pct']:g} % beyond the "
                                f"noise ({m and m['noise']})")
        for name, c in cmp["commands"].items():
            m = c.get("device_us") or c.get("host_ms") or {}
            if name != target and m.get("verdict") == "worse":
                problems.append(f"{name} got slower: {m['before']} -> {m['after']}")
            if c.get("errors") and c["errors"][1] > c["errors"][0]:
                problems.append(f"{name} now answers with errors")
        if (cmp.get("throughput_cps") or {}).get("verdict") == "worse":
            problems.append("throughput dropped")
        if not run["repeatable"]:
            problems.append("the variant's benchmark is not repeatable")
        summary = {n: (c.get("device_us") or c.get("host_ms") or {}).get("change_pct") for n, c in cmp["commands"].items()}
        if problems:
            return self.reject_variant(o, "; ".join(problems), log_fn, deployed=True)
        self.event(o, "done", f"A/B: run #{o['base_run_id']} -> #{run['id']}: {summary} (% change)", cmp, log_fn)
        return self.after_measure(o)

    def after_measure(self, o: dict) -> str:
        return CHECK

    # ------------------------------------------------------------ correctness and security still hold?
    QUICK_FUZZ = {"families": ["boundary", "invalid", "overflow", "oversize"], "max_inputs": 150, "max_resets": 2}

    def stage_check(self, o: dict, log_fn) -> str:
        st = o["state"]
        base = st.get("base_suites", {})
        after = self.run_suites(list(base), f"optimisation:{o['id']}")
        problems, compared = [], {}
        for name, b in base.items():
            a = after.get(name, {})
            if not (b.get("run_id") and a.get("run_id")):
                continue
            cmp = self.s.tests.compare(b["run_id"], a["run_id"])
            compared[name] = {"verdict": cmp["verdict"], "regressed": [x["name"] for x in cmp["regressed"]]}
            problems += [f"{name}/{x['name']}: {x['before']!r} -> {x['after']!r}" for x in cmp["regressed"]]
        fuzz = None
        if o["params"].get("fuzz_check", True):
            camp = self.s.fuzz.start(f"optimisation:{o['id']}", o["project"], self.QUICK_FUZZ["families"], seed=1,
                                     max_inputs=self.QUICK_FUZZ["max_inputs"], max_resets=self.QUICK_FUZZ["max_resets"])
            self.s.jobs.wait(camp["job_id"], 900)
            serious = self.s.fuzz.store.findings(campaign_id=camp["id"], min_severity="medium")
            fuzz = {"campaign_id": camp["id"],
                    "serious": [{k: x[k] for k in ("id", "kind", "severity", "input_text")} for x in serious]}
            problems += [f"fuzz finding #{x['id']} {x['kind']} on {x['input_text'][:40]!r}" for x in serious]
        self.store.set_state(o["id"], check_suites=after, check=compared, fuzz_check=fuzz, check_problems=problems)
        self.event(o, "done" if not problems else "warning",
                   f"correctness: {len(problems)} problems in {len(compared)} suites"
                   + (f" + quick fuzz #{fuzz['campaign_id']}" if fuzz else ""), {"problems": problems}, log_fn)
        return DECIDE

    def stage_decide(self, o: dict, log_fn) -> str:
        problems = o["state"].get("check_problems", [])
        if problems:
            return self.reject_variant(o, "correctness / security: " + "; ".join(problems[:5]), log_fn, deployed=True)
        self.store.update(o["id"], outcome="accepted")
        cmp = o["state"].get("comparison", {})
        self.event(o, "done", f"variant ACCEPTED: build #{o['variant_build']} is measurably better "
                              f"(speed {cmp.get('speed')}, size {o['state'].get('size')}) and still correct",
                   log_fn=log_fn)
        return REPORT

    # ------------------------------------------------------------ roll back: a human decides again
    def stage_rollback(self, o: dict, log_fn) -> Optional[str]:
        try:
            job = self.s.deploys.propose_deploy(o["base_build"], f"optimisation:{o['id']} rollback")
        except ApiError as e:
            self.store.update(o["id"], outcome="rejected_not_rolled_back",
                              error=f"the rollback to build #{o['base_build']} could not be proposed: {e.message}")
            return REPORT
        self.store.update(o["id"], rollback_job_id=job["id"], stage=ROLLED_BACK, status=WAITING)
        self.event({**o, "stage": ROLLBACK}, "waiting", f"redeployment of the baseline build #{o['base_build']} "
                   f"proposed as job #{job['id']} (confirm text '{job['confirm_token']}')", {"job_id": job["id"]},
                   log_fn)
        return None

    def stage_rolled_back(self, o: dict, log_fn) -> Optional[str]:
        job = self._job_decided(o, o["rollback_job_id"])
        if job is None:
            return None
        if job["status"] != "SUCCEEDED":
            self.store.update(o["id"], outcome="rejected_not_rolled_back",
                              error=f"the rollback was not made (job #{job['id']} {job['status']}); the board still "
                                    f"runs the rejected variant #{o['variant_build']}")
            return REPORT
        self.event(o, "done", f"rolled back: the board runs the baseline build #{o['base_build']} again",
                   log_fn=log_fn)
        nxt = self.next_variant(o)
        if nxt == REPORT:
            self.store.update(o["id"], outcome="rejected_rolled_back")
        return nxt

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
