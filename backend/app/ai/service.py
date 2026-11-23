"""Runs agents as background jobs (lane "ai") and pauses / resumes them around human approvals.

A Supervisor run hands tasks to specialists through its `delegate` tool. Each specialist task is a CHILD run
(agent_runs.parent_id) that runs inline, inside the Supervisor's job, with its own checkpoint thread. When a child
stops at a proposal, the Supervisor stops too; resuming the Supervisor first lets the child continue.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..errors import ApiError
from ..jobs import AWAITING, JobContext, JobFailed, JobManager
from .agents import SPECIALISTS, all_specs
from .graph import AgentGraph, AgentSpec
from .llm import BaseLLM, LLMError, UsageLog, make_llm
from .runs import FAILED, FINAL, FINISHED, QUEUED, RUNNING, STOPPED, WAITING, RunStore

log = logging.getLogger("aeisp.ai")

RECURSION_LIMIT = 400
MAX_CHILDREN = 12          # most specialist tasks one Supervisor run may start


class _Delegator:
    """What the Supervisor graph uses to run specialists. Kept small so the graph does not know the service."""

    def __init__(self, service: "AgentService", log_fn) -> None:
        self.s = service
        self.log = log_fn

    def names(self) -> List[str]:
        return list(SPECIALISTS)

    def run(self, parent_id, call_id, agent, task, project, build_id) -> dict:
        return self.s.run_child(parent_id, call_id, agent, task, project, build_id, self.log)

    def resume(self, child_id: int) -> dict:
        return self.s.resume_child(child_id, self.log)

    def result(self, child_id: int) -> dict:
        return self.s.child_result(child_id)


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
        self.specs: Dict[str, AgentSpec] = all_specs()
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
                          max_steps=max_steps, delegator=_Delegator(self, on_step or (lambda t: None)))

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
        if run["parent_id"]:
            raise ApiError(409, "resume_the_parent", f"run {run_id} is part of run {run['parent_id']}; resume that one")
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
        for child in self.store.list(200, parent_id=run_id):
            if child["status"] not in FINAL:
                self.store.finish(child["id"], STOPPED, stop_requested=1, error=f"stopped by {who} (parent run)")
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
        run["children"] = [{"id": r["id"], "agent": r["agent"], "status": r["status"], "outcome": r["outcome"],
                            "goal": r["goal"][:200]} for r in reversed(self.store.list(200, parent_id=run_id))]
        return run

    def tree(self, run_id: int) -> dict:
        """A run with all its child runs, their reports and the tokens of the whole team."""
        run = self.get(run_id)
        kids = list(reversed(self.store.list(200, parent_id=run_id)))
        total = self.usage.totals(run_id)
        nodes = []
        for k in kids:
            u = self.usage.totals(k["id"])
            total["total"] += u["total"]
            total["calls"] += u["calls"]
            nodes.append({"id": k["id"], "agent": k["agent"], "status": k["status"], "outcome": k["outcome"],
                          "task": k["goal"], "steps": k["steps"], "tokens": u["total"], "report": k["report"]})
        return {"id": run["id"], "agent": run["agent"], "goal": run["goal"], "status": run["status"],
                "outcome": run["outcome"], "report": run["report"], "children": nodes, "team_usage": total}

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
                "team": {n: {"description": s.description, "tools": s.tools, "max_steps": s.max_steps}
                         for n, s in self.specs.items()},
                "usage": self.usage.totals(),
                "max_steps": self.settings.ai_max_steps, "max_tokens": self.settings.ai_max_tokens}

    # ------------------------------------------------------------ running one graph
    @staticmethod
    def _config(run: dict) -> dict:
        return {"configurable": {"thread_id": run["thread_id"]}, "recursion_limit": RECURSION_LIMIT}

    def _compiled(self, spec: AgentSpec, on_step=None):
        return self.graph(spec, on_step).compile(self.checkpointer)

    def _invoke(self, run: dict, ag: AgentGraph, payload, log_fn) -> dict:
        """Run (or continue) one agent's graph until it ends or pauses, and record the outcome in agent_runs."""
        graph = ag.compile(self.checkpointer)
        config = self._config(run)
        try:
            graph.invoke(payload, config)
        except LLMError as e:
            self.store.finish(run["id"], FAILED, error=f"the model failed: {e}")
            return {"run_id": run["id"], "status": FAILED, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("agent run %s crashed", run["id"])
            self.store.finish(run["id"], FAILED, error=f"{type(e).__name__}: {e}")
            return {"run_id": run["id"], "status": FAILED, "error": f"{type(e).__name__}: {e}"}
        snap = graph.get_state(config)
        values = snap.values or {}
        if snap.next:                                       # paused at an approval (its own or a child's)
            job_id = values.get("pending_job")
            child_id = values.get("pending_child")
            if child_id and not job_id:
                job_id = (self.store.get(child_id) or {}).get("pending_job_id")
            self.store.update(run["id"], status=WAITING, pending_job_id=job_id)
            log_fn(f"agent run {run['id']} is waiting for a human to decide on job #{job_id}")
            return {"run_id": run["id"], "status": WAITING, "pending_job_id": job_id, "child_run_id": child_id}
        report = values.get("report") or {}
        stop = values.get("stop_reason")
        status = STOPPED if stop and "operator" in stop else FINISHED
        self.store.finish(run["id"], status, outcome=report.get("outcome"), answer=values.get("answer", ""),
                          report_json=json.dumps(report), error=stop)
        log_fn(f"agent run {run['id']} ({run['agent']}) {status}: {report.get('outcome')} - "
               f"{report.get('summary', '')[:200]}")
        return {"run_id": run["id"], "status": status, "outcome": report.get("outcome"), "steps": values.get("steps"),
                "tokens": values.get("tokens"), "error": stop}

    # ------------------------------------------------------------ job runners
    def _finish_job(self, out: dict) -> dict:
        if out["status"] == FAILED:
            raise JobFailed(f"agent run {out['run_id']} failed: {out.get('error')}", out)
        if out["status"] == STOPPED or out.get("outcome") == "failed":
            run = self.store.get(out["run_id"]) or {}
            summary = (run.get("report") or {}).get("summary") or out.get("error")
            raise JobFailed(f"agent run {out['run_id']} ended: {summary}", out)
        return out

    def _run_job(self, ctx: JobContext) -> dict:
        run = self.get(ctx.params["run_id"])
        spec = self._spec(run["agent"])
        ag = self.graph(spec, ctx.log, run["max_steps"])
        self.store.update(run["id"], status=RUNNING, prompts_json=json.dumps(ag.prompt_refs()))
        ctx.log(f"agent run {run['id']} ({spec.name}) started: {run['goal'][:120]}")
        state = ag.initial_state(run["id"], run["goal"], run["params"].get("context", ""))
        return self._finish_job(self._invoke(run, ag, state, ctx.log))

    def _resume_job(self, ctx: JobContext) -> dict:
        run = self.get(ctx.params["run_id"])
        ag = self.graph(self._spec(run["agent"]), ctx.log, run["max_steps"])
        self.store.update(run["id"], status=RUNNING, pending_job_id=None)
        ctx.log(f"agent run {run['id']} resumed")
        return self._finish_job(self._invoke(run, ag, Command(resume={"by": "operator decision"}), ctx.log))

    # ------------------------------------------------------------ child runs (specialists)
    def child_task(self, parent: dict, agent: str, task: str, project: Optional[str], build_id: Optional[int]) -> str:
        lines = [task]
        if project:
            lines.append(f"Project: {project}")
        if build_id:
            lines.append(f"Build: #{build_id}")
        return "\n".join(lines)

    def child_result(self, child_id: int) -> dict:
        c = self.get(child_id)
        return {"child_run_id": c["id"], "agent": c["agent"], "status": c["status"], "outcome": c["outcome"],
                "steps": c["steps"], "report": c["report"], "error": c["error"]}

    def run_child(self, parent_id: int, call_id: str, agent: str, task: str, project: Optional[str],
                  build_id: Optional[int], log_fn) -> dict:
        parent = self.get(parent_id)
        for existing in self.store.list(200, parent_id=parent_id):      # the same call again (after a restart)
            if existing["params"].get("call_id") == call_id:
                return self.child_result(existing["id"]) if existing["status"] != WAITING else \
                    {**self.child_result(existing["id"]), "job_id": existing["pending_job_id"]}
        if len(self.store.list(200, parent_id=parent_id)) >= MAX_CHILDREN:
            raise ApiError(409, "too_many_tasks", f"this run already started {MAX_CHILDREN} specialist tasks")
        spec = self._spec(agent)
        goal = self.child_task(parent, agent, task, project, build_id)
        child = self.store.create(spec.name, goal, f"run:{parent_id}", spec.max_steps or self.settings.ai_max_steps,
                                  {"call_id": call_id, "project": project, "build_id": build_id}, parent_id)
        ag = self.graph(spec, log_fn, child["max_steps"])
        self.store.update(child["id"], status=RUNNING, prompts_json=json.dumps(ag.prompt_refs()))
        self.store.add_step(parent_id, "delegate", agent, None, {"child_run_id": child["id"], "task": goal[:1500]})
        log_fn(f"  -> {agent} agent (run {child['id']}): {task[:120]}")
        out = self._invoke(child, ag, ag.initial_state(child["id"], goal), log_fn)
        result = self.child_result(child["id"])
        if out["status"] == WAITING:
            result["job_id"] = out["pending_job_id"]
        return result

    def resume_child(self, child_id: int, log_fn) -> dict:
        child = self.get(child_id)
        if child["status"] != WAITING:
            return self.child_result(child_id)
        ag = self.graph(self._spec(child["agent"]), log_fn, child["max_steps"])
        self.store.update(child_id, status=RUNNING, pending_job_id=None)
        out = self._invoke(child, ag, Command(resume={"by": "operator decision"}), log_fn)
        result = self.child_result(child_id)
        if out["status"] == WAITING:
            result["job_id"] = out["pending_job_id"]
        return result
