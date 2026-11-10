"""The tool registry: the ONLY way an AI agent acts on the platform.

An agent never runs commands. It calls a named tool with arguments; the registry checks the caller's role,
validates the arguments against a schema, runs the tool, and writes an audit entry. Tools that change the world
in a risky way (deploying, recovering) do not do it: they only PROPOSE, and a human must approve.

There is deliberately NO tool for approving a job or promoting known-good firmware. Those exist only as
operator-only HTTP endpoints.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, Field, ValidationError

from .errors import ApiError
from .security import Principal
from .services.testing import RunRequest

Handler = Callable[[Any, Principal, BaseModel], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: Type[BaseModel]
    min_role: str              # viewer | agent | operator
    risk: str                  # read | write | propose
    handler: Handler
    audit_skip: tuple = ()     # argument names never written to the audit log (e.g. big file contents)


# ---------------------------------------------------------------- argument models (descriptions are shown to the AI)
class NoArgs(BaseModel):
    pass


class ProjectArg(BaseModel):
    project: str = Field(description="firmware project name, e.g. 'demo'")


class FileArg(ProjectArg):
    filename: str = Field(description="file inside the project's app folder, e.g. 'app.c'")


class WriteSourceArgs(FileArg):
    content: str = Field(max_length=65536, description="the COMPLETE new text of the file")


class CreateProjectArgs(BaseModel):
    name: str = Field(description="new project name: lower-case letters, digits and '_', starting with a letter")


class RestoreArgs(ProjectArg):
    build_id: int = Field(ge=1, description="restore the sources exactly as they were in this build")


class StartBuildArgs(ProjectArg):
    timeout_s: Optional[int] = Field(None, ge=10, le=900, description="give up after this many seconds")


class BuildIdArg(BaseModel):
    build_id: int = Field(ge=1)


class BuildHistoryArgs(BaseModel):
    project: Optional[str] = Field(None, description="only builds of this project")
    limit: int = Field(10, ge=1, le=50)


class JobIdArg(BaseModel):
    job_id: int = Field(ge=1)


class JobLogsArgs(JobIdArg):
    after: int = Field(0, ge=0, description="only lines after this line number (from a previous call)")


class WaitArgs(JobIdArg):
    timeout_s: int = Field(60, ge=1, le=120, description="how long to wait for the job to finish")


class CommandArgs(BaseModel):
    command: str = Field(min_length=1, max_length=120, description="one protocol line, e.g. 'PING' or 'LED ON'")


class ProposeDeployArgs(BaseModel):
    build_id: int = Field(ge=1, description="a SUCCESSFUL build to put on the board")


class ProposeRecoverArgs(BaseModel):
    target: Literal["auto", "known-good", "stock"] = Field("auto", description="which firmware to restore")


class LimitArg(BaseModel):
    limit: int = Field(10, ge=1, le=50)


class RunIdArg(BaseModel):
    run_id: int = Field(ge=1)


# ---------------------------------------------------------------- JSON schema for the model (Gemini function declarations)
def _clean(schema: dict) -> dict:
    """Reduce a pydantic JSON schema to the simple subset function-calling APIs accept."""
    if "anyOf" in schema:                                   # Optional[X] -> X, nullable
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        base = _clean(options[0]) if options else {"type": "string"}
        base["nullable"] = True
        if "description" in schema:
            base["description"] = schema["description"]
        return base
    out: dict = {}
    for key in ("type", "description", "enum", "minimum", "maximum", "minLength", "maxLength"):
        if key in schema:
            out[key] = schema[key]
    if schema.get("type") == "object":
        out["properties"] = {k: _clean(v) for k, v in schema.get("properties", {}).items()}
        if schema.get("required"):
            out["required"] = list(schema["required"])
    if schema.get("type") == "array" and "items" in schema:
        out["items"] = _clean(schema["items"])
    return out


def parameters_schema(tool: Tool) -> Optional[dict]:
    """None for tools without arguments (the API wants the field left out)."""
    schema = tool.params.model_json_schema()
    if not schema.get("properties"):
        return None
    defs = schema.get("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].split("/")[-1]])
            return {k: inline(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [inline(x) for x in node]
        return node

    return _clean(inline(schema))


# ---------------------------------------------------------------- the registry
class ToolRegistry:
    def __init__(self, services) -> None:
        self.s = services
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def names(self) -> List[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        t = self._tools.get(name)
        if t is None:
            raise ApiError(404, "unknown_tool", f"unknown tool {name!r}")
        return t

    def visible_to(self, principal: Principal) -> List[Tool]:
        return [t for t in self._tools.values() if principal.at_least(t.min_role)]

    def describe(self, principal: Principal) -> List[dict]:
        return [{"name": t.name, "description": t.description, "min_role": t.min_role, "risk": t.risk,
                 "parameters": parameters_schema(t)} for t in sorted(self.visible_to(principal), key=lambda x: x.name)]

    def declarations(self, principal: Principal) -> List[dict]:
        """Function declarations in the shape Gemini expects (used by the AI layer)."""
        out = []
        for t in sorted(self.visible_to(principal), key=lambda x: x.name):
            d = {"name": t.name, "description": t.description}
            params = parameters_schema(t)
            if params:
                d["parameters"] = params
            out.append(d)
        return out

    def call(self, name: str, arguments: Optional[dict], principal: Principal) -> Any:
        tool = self.get(name)
        if not principal.at_least(tool.min_role):
            self.s.audit.write(principal.name, principal.role, f"tool.{name}", "", "refused:forbidden")
            raise ApiError(403, "forbidden", f"tool '{name}' needs the '{tool.min_role}' role")
        try:
            args = tool.params.model_validate(arguments or {})
        except ValidationError as e:
            self.s.audit.write(principal.name, principal.role, f"tool.{name}", "", "refused:invalid_arguments")
            raise ApiError(422, "invalid_arguments", f"the arguments for '{name}' are not valid",
                           [{"where": ".".join(str(x) for x in err["loc"]), "problem": err["msg"]}
                            for err in e.errors()])
        shown = {k: v for k, v in args.model_dump().items() if k not in tool.audit_skip}
        target = json.dumps(shown, default=str)[:160]
        try:
            result = tool.handler(self.s, principal, args)
        except ApiError as e:
            self.s.audit.write(principal.name, principal.role, f"tool.{name}", target, f"error:{e.code}", e.message)
            raise
        except Exception as e:  # noqa: BLE001  other exceptions are mapped to HTTP errors by the API layer
            self.s.audit.write(principal.name, principal.role, f"tool.{name}", target, f"error:{type(e).__name__}", str(e))
            raise
        self.s.audit.write(principal.name, principal.role, f"tool.{name}", target, "ok")
        return result


# ---------------------------------------------------------------- the tools
def _who(p: Principal) -> str:
    return f"tool:{p.name}"


def _wait_for_job(s, p, a: WaitArgs):
    try:
        job = s.jobs.wait(a.job_id, float(a.timeout_s))
        return {"finished": True, "job": job}
    except TimeoutError:
        job = s.jobs.get(a.job_id)
        if job is None:
            raise ApiError(404, "not_found", f"no job {a.job_id}")
        return {"finished": False, "job": job, "note": f"still {job['status']} after {a.timeout_s} s; ask again"}


def _job_or_404(s, job_id: int) -> dict:
    job = s.jobs.get(job_id)
    if job is None:
        raise ApiError(404, "not_found", f"no job {job_id}")
    return job


def build_registry(s) -> ToolRegistry:
    r = ToolRegistry(s)
    T = r.register

    T(Tool("device_status", "Is the board connected, and which firmware is it running? (read-only)",
           NoArgs, "viewer", "read", lambda s, p, a: s.device.status(_who(p))))
    T(Tool("device_command",
           "Send ONE safe protocol command to the board and get its reply. Allowed: HELLO PING ECHO UPTIME VERSION "
           "INFO STATUS LED BLINK TIMER TEMP RESETINFO APP. Cannot reboot, hang or flash the board.",
           CommandArgs, "agent", "write", lambda s, p, a: s.device.command(a.command, _who(p), operator=False)))

    T(Tool("list_projects", "List the firmware projects, their source files and their latest build.",
           NoArgs, "viewer", "read", lambda s, p, a: s.firmware.list_projects()))
    T(Tool("create_project", "Create a new firmware project with a starter app.c.",
           CreateProjectArgs, "agent", "write", lambda s, p, a: s.firmware.create_project(a.name)))
    T(Tool("list_sources", "List the source files of a project (name, size, sha256).",
           ProjectArg, "viewer", "read", lambda s, p, a: s.firmware.list_sources(a.project)))
    T(Tool("read_source", "Read one source file of a project.",
           FileArg, "viewer", "read", lambda s, p, a: s.firmware.read_source(a.project, a.filename)))
    T(Tool("write_source", "Create or completely replace one source file (.c or .h) of a project. The change is "
           "atomic. Include the whole file content.",
           WriteSourceArgs, "agent", "write",
           lambda s, p, a: s.firmware.write_source(a.project, a.filename, a.content), audit_skip=("content",)))
    T(Tool("delete_source", "Delete one source file of a project.",
           FileArg, "agent", "write", lambda s, p, a: s.firmware.delete_source(a.project, a.filename)))
    T(Tool("restore_sources", "Put a project's source files back exactly as they were when a given build was made.",
           RestoreArgs, "agent", "write", lambda s, p, a: s.firmware.restore_sources(a.project, a.build_id)))

    T(Tool("start_build", "Start a build of a project (takes 30-90 s). Returns a job; use wait_for_job to follow it.",
           StartBuildArgs, "agent", "write", lambda s, p, a: s.builds.start(a.project, _who(p), a.timeout_s)))
    T(Tool("get_build", "Details of one finished build: status, compiler errors and warnings with file and line, sizes.",
           BuildIdArg, "viewer", "read", lambda s, p, a: s.builds.get(a.build_id)))
    T(Tool("build_history", "Recent builds, newest first.",
           BuildHistoryArgs, "viewer", "read", lambda s, p, a: s.builds.history(a.project, a.limit)))

    T(Tool("get_job", "Status and result of a background job.",
           JobIdArg, "viewer", "read", lambda s, p, a: _job_or_404(s, a.job_id)))
    T(Tool("get_job_logs", "Progress lines of a background job.",
           JobLogsArgs, "viewer", "read", lambda s, p, a: s.jobs.logs(_job_or_404(s, a.job_id)["id"], a.after)))
    T(Tool("wait_for_job", "Wait (up to timeout_s) for a background job to finish and return its final state.",
           WaitArgs, "viewer", "read", _wait_for_job))

    T(Tool("propose_deployment",
           "PROPOSE flashing a successful build onto the board. This only runs read-only checks and creates a job "
           "that waits for a human operator to approve it. It does NOT flash anything by itself.",
           ProposeDeployArgs, "agent", "propose", lambda s, p, a: s.deploys.propose_deploy(a.build_id, _who(p))))
    T(Tool("propose_recovery",
           "PROPOSE restoring the board to known-good or factory firmware. Waits for a human operator to approve.",
           ProposeRecoverArgs, "agent", "propose", lambda s, p, a: s.deploys.propose_recover(a.target, _who(p))))
    T(Tool("deployment_history", "Recent deployments and recoveries with their outcome.",
           LimitArg, "viewer", "read", lambda s, p, a: s.deploys.history(a.limit)))
    T(Tool("known_good_info", "Which firmware the board falls back to, and which boards are registered.",
           NoArgs, "viewer", "read", lambda s, p, a: s.deploys.known_good()))

    T(Tool("run_tests", "Run checks against the board as a job. Give either suite='smoke' or your own cases "
           "(name, command, expect).",
           RunRequest, "agent", "write", lambda s, p, a: s.tests.start(a, _who(p))))
    T(Tool("get_test_run", "Result of a finished test run, check by check.",
           RunIdArg, "viewer", "read", lambda s, p, a: s.tests.get_run(a.run_id)))
    T(Tool("list_test_runs", "Recent test runs.",
           LimitArg, "viewer", "read", lambda s, p, a: s.tests.list_runs(a.limit)))
    return r