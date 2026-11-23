"""Runs agents as background jobs (lane "ai") and pauses / resumes them around human approvals."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..errors import ApiError
from ..jobs import AWAITING, JobContext, JobFailed, JobManager
from .graph import LAB_AGENT, AgentGraph, AgentSpec
from .llm import BaseLLM, LLMError, UsageLog, make_llm
from .runs import FAILED, FINAL, FINISHED, QUEUED, RUNNING, STOPPED, WAITING, RunStore

log = logging.getLogger("aeisp.ai")

RECURSION_LIMIT = 400


class AgentService:
    def __init__(self, settings, jobs: JobManager, tools, llm_factory: Optional[Callable[[], BaseLLM]] = None,
                 checkpoint_path: Optional[Path] = None) -> None:
        self.settings = settings
        self.jobs = jobs
        self.tools = tools
        self.store = RunStore(settings.db_path)
        self.usage = UsageLog(settings.db_path)
        self._custom_llm = llm_factory is not None
        self._llm_factory = llm_factory or (lambda: make_llm(settings, self.usage))
        self._llm: Optional[BaseLLM] = None
        self.specs: Dict[str, AgentSpec] = {LAB_AGENT.name: LAB_AGENT}
        path = Path(checkpoint_path or settings.checkpoint_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self.checkpointer = SqliteSaver(self._conn)
        self._lock = threading.Lock()
        self.store.recover_orphans()
        jobs.register("agent_run", self._run_job, "ai")
        jobs.register("agent_resume", self._resume_job, "ai")

    # ------------------------------------------------------------ setup
    def register(self, spec: AgentSpec) -> None:
        self.specs[spec.name] = spec

    def llm(self) -> BaseLLM:
        with self._lock:
            if self._llm is None:
                self._llm = self._llm_factory()
                if self._llm.usage_log is None:
                    self._llm.usage_log = self.usage
            return self._llm

    @property
    def configured(self) -> bool:
        """A key in .env, or a model handed in directly (tests and tools)."""
        return self.settings.ai_configured or self._custom_llm

    def graph(self, spec: AgentSpec, on_step=None, max_steps: Optional[int] = None) -> AgentGraph:
        return AgentGraph(spec, self.llm(), self.tools, self.jobs, self.store, self.settings, on_step=on_step,
                          max_steps=max_steps)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ starting / resuming / stopping
    def _spec(self, agent: str) -> AgentSpec:
        spec = self.specs.get(agent)
        if spec is None:
            raise ApiError(404, "unknown_agent", f"no agent {agent!r}; known: {', '.join(sorted(self.specs))}")
        return spec

    def _require_model(self) -> None:
        if not self.configured:
            raise ApiError(503, "ai_not_configured", "GEMINI_API_KEY is not set in .env; the agents cannot run")

    def start(self, goal: str, requested_by: str, agent: str = "lab", max_steps: Optional[int] = None,
              context: str = "", parent_id: Optional[int] = None) -> dict:
        self._require_model()
        spec = self._spec(agent)
        steps = max_steps or spec.max_steps or self.settings.ai_max_steps
        run = self.store.create(spec.name, goal, requested_by, steps, {"context": context}, parent_id)
        job = self.jobs.submit("agent_run", {"run_id": run["id"]}, requested_by,
                               summary=f"{spec.name} agent: {goal[:80]}")
        return {**self.store.get(run["id"]), "job_id": job["id"]}

    def resume(self, run_id: int, requested_by: str) -> dict:
        run = self.get(run_id)
        if run["status"] != WAITING:
            raise ApiError(409, "run_not_waiting", f"run {run_id} is {run['status']}, not waiting for approval")
        job = self.jobs.get(run["pending_job_id"]) if run["pending_job_id"] else None
        if job is not None and job["status"] == AWAITING:
            raise ApiError(409, "still_awaiting_approval",
                           f"proposal job #{job['id']} still waits for a human; approve or reject it first "
                           f"(POST /jobs/{job['id']}/approve or /reject)")
        self._require_model()
        self.store.update(run_id, status=QUEUED)
        job = self.jobs.submit("agent_resume", {"run_id": run_id}, requested_by, summary=f"resume agent run {run_id}")
        return {**self.store.get(run_id), "job_id": job["id"]}

    def stop(self, run_id: int, who: str) -> dict:
        run = self.get(run_id)
        if run["status"] in FINAL:
            raise ApiError(409, "run_finished", f"run {run_id} is already {run['status']}")
        if run["status"] == WAITING:
            self.store.finish(run_id, STOPPED, stop_requested=1, error=f"stopped by {who} while waiting for approval")
        else:
            self.store.update(run_id, stop_requested=1)
        self.store.add_step(run_id, "stop", who, None, {"by": who})
        return self.store.get(run_id)  # type: ignore[return-value]

    # ------------------------------------------------------------ reading
    def get(self, run_id: int) -> dict:
        run = self.store.get(run_id)
        if run is None:
            raise ApiError(404, "not_found", f"no agent run {run_id}")
        return run

    def details(self, run_id: int) -> dict:
        run = self.get(run_id)
        run["usage"] = self.usage.totals(run_id)
        run["children"] = [{"id": r["id"], "agent": r["agent"], "status": r["status"], "outcome": r["outcome"]}
                           for r in self.store.list(50, parent_id=run_id)]
        return run

    def messages(self, run_id: int) -> list:
        """The conversation from the checkpoint (for debugging; signatures are left out)."""
        run = self.get(run_id)
        snap = self._compiled(self._spec(run["agent"])).get_state(self._config(run))
        out = []
        for m in (snap.values or {}).get("messages", []):
            m = dict(m)
            if m.get("calls"):
                m["calls"] = [{k: v for k, v in c.items() if k != "sig"} for c in m["calls"]]
            out.append(m)
        return out

    def status(self) -> dict:
        return {"configured": self.configured, "model": self.settings.gemini_model, "agents": sorted(self.specs),
                "usage": self.usage.totals(),
                "max_steps": self.settings.ai_max_steps, "max_tokens": self.settings.ai_max_tokens}

    # ------------------------------------------------------------ running (job runners)
    @staticmethod
    def _config(run: dict) -> dict:
        return {"configurable": {"thread_id": run["thread_id"]}, "recursion_limit": RECURSION_LIMIT}

    def _compiled(self, spec: AgentSpec, on_step=None):
        return self.graph(spec, on_step).compile(self.checkpointer)

    def _run_job(self, ctx: JobContext) -> dict:
        run = self.get(ctx.params["run_id"])
        spec = self._spec(run["agent"])
        ag = self.graph(spec, ctx.log, run["max_steps"])
        self.store.update(run["id"], status=RUNNING, prompts_json=json.dumps(ag.prompt_refs()))
        ctx.log(f"agent run {run['id']} ({spec.name}) started: {run['goal'][:120]}")
        state = ag.initial_state(run["id"], run["goal"], run["params"].get("context", ""))
        return self._drive(ctx, run, ag, state)

    def _resume_job(self, ctx: JobContext) -> dict:
        run = self.get(ctx.params["run_id"])
        ag = self.graph(self._spec(run["agent"]), ctx.log, run["max_steps"])
        self.store.update(run["id"], status=RUNNING, pending_job_id=None)
        ctx.log(f"agent run {run['id']} resumed")
        return self._drive(ctx, run, ag, Command(resume={"by": "operator decision"}))

    def _drive(self, ctx: JobContext, run: dict, ag: AgentGraph, payload) -> dict:
        graph = ag.compile(self.checkpointer)
        config = self._config(run)
        try:
            graph.invoke(payload, config)
        except LLMError as e:
            self.store.finish(run["id"], FAILED, error=f"the model failed: {e}")
            raise JobFailed(f"agent run {run['id']} failed: {e}", {"run_id": run["id"]})
        except Exception as e:  # noqa: BLE001
            log.exception("agent run %s crashed", run["id"])
            self.store.finish(run["id"], FAILED, error=f"{type(e).__name__}: {e}")
            raise JobFailed(f"agent run {run['id']} crashed: {type(e).__name__}: {e}", {"run_id": run["id"]})
        snap = graph.get_state(config)
        values = snap.values or {}
        if snap.next:                                       # paused at the approval node
            job_id = values.get("pending_job")
            self.store.update(run["id"], status=WAITING, pending_job_id=job_id)
            ctx.log(f"agent run {run['id']} is waiting for a human to decide on job #{job_id}")
            return {"run_id": run["id"], "status": WAITING, "pending_job_id": job_id}
        report = values.get("report") or {}
        stop = values.get("stop_reason")
        status = STOPPED if stop and "operator" in stop else FINISHED
        self.store.finish(run["id"], status, outcome=report.get("outcome"), answer=values.get("answer", ""),
                          report_json=json.dumps(report), error=stop)
        ctx.log(f"agent run {run['id']} {status}: {report.get('outcome')} - {report.get('summary', '')[:200]}")
        out = {"run_id": run["id"], "status": status, "outcome": report.get("outcome"), "steps": values.get("steps"),
               "tokens": values.get("tokens")}
        if report.get("outcome") == "failed" or status == STOPPED:
            raise JobFailed(f"agent run {run['id']} ended: {report.get('summary') or stop}", out)
        return out
