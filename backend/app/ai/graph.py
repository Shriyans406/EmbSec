"""The agent loop as a LangGraph state graph.

        START -> model --(tool calls)--> tools --(a proposal was made)--> approval --(human decided)--> model
                  |  ^                     |                                  (pauses the run with interrupt)
                  |  +---------------------+
                  +--(plain answer, stop, budget used up)--> report -> END

- model:    asks the LLM for the next step, with the tools this agent may use.
- tools:    runs the calls through the tool registry AS THE AGENT ROLE. The registry checks the role, validates the
            arguments and writes the audit log. Errors are given back to the model as data.
- approval: a deployment/recovery proposal needs a human. The graph stops here (LangGraph interrupt) and the run
            is WAITING_APPROVAL. A human approves or rejects the job through the operator API; resuming the run
            only lets the agent SEE that decision. There is no way for the agent to approve.
- report:   a structured summary of the run, made from the transcript (structured JSON output).

The state is checkpointed after every node (SqliteSaver), so a paused run survives a server restart.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..errors import ApiError
from ..jobs import AWAITING, FINAL as JOB_FINAL
from ..security import Principal
from . import prompts
from .llm import BaseLLM, LLMError
from .runs import RunStore

log = logging.getLogger("aeisp.ai")

AGENT_PRINCIPAL = Principal("agent", "agent")     # every agent acts with the agent role, whoever started the run
PROPOSAL_KINDS = {"deploy", "recover"}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["success", "partial", "waiting_for_human", "failed"]},
        "summary": {"type": "string", "description": "two or three sentences"},
        "facts": {"type": "array", "items": {"type": "string"}, "description": "what the tool results show"},
        "changes": {"type": "array", "items": {"type": "string"}, "description": "files written, builds, jobs"},
        "unverified": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "summary", "facts", "changes", "unverified", "next_steps"],
}


class AgentState(TypedDict, total=False):
    run_id: int
    goal: str
    messages: list
    steps: int
    tokens: int
    pending_job: Optional[int]
    stop_reason: Optional[str]
    answer: str
    report: Optional[dict]
    wrap_up_asked: bool


@dataclass
class AgentSpec:
    """What one agent is: its prompt and the tools it may call (None = every tool of the agent role)."""
    name: str
    prompt: str
    tools: Optional[List[str]] = None
    description: str = ""
    max_steps: Optional[int] = None
    extra: dict = field(default_factory=dict)


LAB_AGENT = AgentSpec("lab", "lab_agent", None, "general lab agent with every agent-role tool")


def _shorten(value, limit: int):
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "chars": len(text), "preview": text[:limit]}


def _shown_args(name: str, args: dict) -> dict:
    out = dict(args)
    if name == "write_source" and isinstance(out.get("content"), str):
        out["content"] = f"<{len(out['content'])} chars>"
    return out


def transcript(state: dict, limit: int = 60000) -> str:
    """The conversation as plain text, for the report (keeps the newest part if it is too long)."""
    lines = [f"GOAL: {state.get('goal', '')}"]
    for m in state.get("messages", [])[1:]:
        if m["role"] == "model":
            if m.get("text"):
                lines.append(f"AGENT: {m['text']}")
            for c in m.get("calls", []):
                lines.append(f"TOOL CALL {c['name']}: {json.dumps(_shown_args(c['name'], c.get('args') or {}))}")
        elif m["role"] == "tool":
            for r in m["results"]:
                lines.append(f"TOOL RESULT {r['name']}: {json.dumps(r['response'], default=str)[:3000]}")
        else:
            lines.append(f"PLATFORM: {m['text']}")
    text = "\n".join(lines)
    return text if len(text) <= limit else "...(older part cut)...\n" + text[-limit:]


class AgentGraph:
    def __init__(self, spec: AgentSpec, llm: BaseLLM, tools, jobs, store: RunStore, settings,
                 approval_wait_s: float = 900.0, on_step: Optional[Callable[[str], None]] = None) -> None:
        self.spec = spec
        self.llm = llm
        self.tools = tools
        self.jobs = jobs
        self.store = store
        self.settings = settings
        self.max_steps = spec.max_steps or settings.ai_max_steps
        self.approval_wait_s = approval_wait_s
        self.on_step = on_step or (lambda text: None)
        self.prompt = prompts.load(spec.prompt)
        self.report_prompt = prompts.load("run_report")
        self.system = self.prompt.render(max_steps=self.max_steps)
        allowed = None if spec.tools is None else set(spec.tools)
        self.declarations = [d for d in tools.declarations(AGENT_PRINCIPAL) if allowed is None or d["name"] in allowed]
        self.allowed = {d["name"] for d in self.declarations}

    def prompt_refs(self) -> list:
        return [self.prompt.ref(), self.report_prompt.ref()]

    # ------------------------------------------------------------ nodes
    def model_node(self, state: AgentState) -> dict:
        run_id = state["run_id"]
        if self.store.stop_requested(run_id):
            return {"stop_reason": "stopped by an operator"}
        if state.get("tokens", 0) >= self.settings.ai_max_tokens:
            return {"stop_reason": f"the token budget ({self.settings.ai_max_tokens}) is used up"}
        messages = list(state["messages"])
        out_of_steps = state.get("steps", 0) >= self.max_steps
        update: dict = {}
        if out_of_steps and not state.get("wrap_up_asked"):
            messages.append({"role": "user", "text": f"[platform] The step limit ({self.max_steps}) is reached. "
                                                     "Do not call tools. Summarise what was done and what is left."})
            update["wrap_up_asked"] = True
        try:
            reply = self.llm.generate(self.system, messages, None if out_of_steps else self.declarations,
                                      purpose=f"{self.spec.name}:step", run_id=run_id)
        except LLMError as e:
            self.store.add_step(run_id, "model", "error", False, {"error": str(e)})
            return {**update, "messages": messages, "stop_reason": f"the model failed: {e}"}
        if out_of_steps:
            reply.calls = []
        messages.append(reply.as_message())
        tokens = state.get("tokens", 0) + reply.usage.total
        self.store.add_step(run_id, "model", "reply", True,
                            {"text": reply.text[:1500], "calls": [c.name for c in reply.calls],
                             "tokens": reply.usage.total, "finish": reply.finish_reason})
        self.store.update(run_id, tokens=tokens)
        if reply.text:
            self.on_step(f"agent: {reply.text[:200]}")
        update.update({"messages": messages, "tokens": tokens})
        if not reply.calls:
            update["answer"] = reply.text
        return update

    def _call_one(self, run_id: int, name: str, args: dict) -> dict:
        if name not in self.allowed:
            return {"error": {"code": "tool_not_allowed", "message": f"the {self.spec.name} agent may not use '{name}'"}}
        try:
            result = self.tools.call(name, args, AGENT_PRINCIPAL)
            return {"result": _shorten(result, self.settings.ai_tool_result_chars)}
        except ApiError as e:
            return {"error": {"code": e.code, "message": e.message,
                              "details": _shorten(e.details, 1500) if e.details else None}}
        except Exception as e:  # noqa: BLE001  the model is told; the run goes on
            log.exception("tool %s crashed", name)
            return {"error": {"code": "internal", "message": f"{type(e).__name__}: {e}"}}

    def tools_node(self, state: AgentState) -> dict:
        run_id = state["run_id"]
        calls = state["messages"][-1].get("calls", [])
        steps = state.get("steps", 0)
        results, pending = [], state.get("pending_job")
        for c in calls:
            if steps >= self.max_steps:
                response = {"error": {"code": "step_limit", "message": "the step limit is reached; this call was not run"}}
            else:
                steps += 1
                response = self._call_one(run_id, c["name"], c.get("args") or {})
            ok = "error" not in response
            res = response.get("result")
            if ok and isinstance(res, dict) and res.get("status") == AWAITING and res.get("kind") in PROPOSAL_KINDS:
                pending = res["id"]
            self.store.add_step(run_id, "tool", c["name"], ok,
                                {"args": _shown_args(c["name"], c.get("args") or {}),
                                 "response": _shorten(response, 1500)})
            self.on_step(f"tool {c['name']}: {'ok' if ok else response['error']['code']}")
            results.append({"id": c.get("id"), "name": c["name"], "response": response})
        self.store.update(run_id, steps=steps)
        return {"messages": state["messages"] + [{"role": "tool", "results": results}], "steps": steps,
                "pending_job": pending}

    def approval_node(self, state: AgentState) -> dict:
        run_id, job_id = state["run_id"], state["pending_job"]
        job = self.jobs.get(job_id)
        if job is not None and job["status"] == AWAITING:
            self.store.add_step(run_id, "approval", "waiting", None,
                                {"job_id": job_id, "kind": job["kind"], "summary": job.get("summary")})
            interrupt({"job_id": job_id, "kind": job["kind"], "summary": job.get("summary")})
            job = self.jobs.get(job_id)
        if job is not None and job["status"] not in JOB_FINAL and job["status"] != AWAITING:
            try:                                  # approved: let the deployment finish before the agent looks
                job = self.jobs.wait(job_id, self.approval_wait_s)
            except TimeoutError:
                job = self.jobs.get(job_id)
        status = job["status"] if job else "MISSING"
        note = {"job_id": job_id, "status": status, "approved_by": job.get("approved_by") if job else None,
                "error": job.get("error") if job else None,
                "result": _shorten(job.get("result"), 2500) if job else None}
        self.store.add_step(run_id, "approval", "decided", status == "SUCCEEDED", note)
        text = (f"[platform] A human has handled proposal job #{job_id}. It is now {status}. "
                f"Details: {json.dumps(note, default=str)}")
        return {"messages": state["messages"] + [{"role": "user", "text": text}], "pending_job": None}

    def report_node(self, state: AgentState) -> dict:
        run_id = state["run_id"]
        stop = state.get("stop_reason")
        ask = transcript(state)
        if stop:
            ask += f"\nPLATFORM: the run was ended early: {stop}"
        try:
            report, _ = self.llm.generate_json(self.report_prompt.text, [{"role": "user", "text": ask}], REPORT_SCHEMA,
                                               purpose=f"{self.spec.name}:report", run_id=run_id)
        except LLMError as e:
            report = {"outcome": "failed", "summary": f"no report: the model failed ({e})", "facts": [],
                      "changes": [], "unverified": [], "next_steps": []}
        self.store.add_step(run_id, "report", report.get("outcome", ""), True, report)
        return {"report": report}

    # ------------------------------------------------------------ edges
    @staticmethod
    def after_model(state: AgentState) -> str:
        if state.get("stop_reason"):
            return "report"
        last = state["messages"][-1]
        return "tools" if last["role"] == "model" and last.get("calls") else "report"

    @staticmethod
    def after_tools(state: AgentState) -> str:
        return "approval" if state.get("pending_job") else "model"

    def compile(self, checkpointer):
        g = StateGraph(AgentState)
        g.add_node("model", self.model_node)
        g.add_node("tools", self.tools_node)
        g.add_node("approval", self.approval_node)
        g.add_node("report", self.report_node)
        g.add_edge(START, "model")
        g.add_conditional_edges("model", self.after_model, {"tools": "tools", "report": "report"})
        g.add_conditional_edges("tools", self.after_tools, {"approval": "approval", "model": "model"})
        g.add_edge("approval", "model")
        g.add_edge("report", END)
        return g.compile(checkpointer=checkpointer)

    def initial_state(self, run_id: int, goal: str, context: str = "") -> AgentState:
        text = goal if not context else f"{goal}\n\nContext:\n{context}"
        return {"run_id": run_id, "goal": goal, "messages": [{"role": "user", "text": text}], "steps": 0,
                "tokens": 0, "pending_job": None, "stop_reason": None, "answer": "", "report": None,
                "wrap_up_asked": False}
