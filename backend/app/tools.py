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
from .services.testing import RunRequest, TestCase

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


class ScanArgs(BaseModel):
    project: Optional[str] = Field(None, description="scan the project's current sources ...")
    build_id: Optional[int] = Field(None, ge=1, description="... or the sources exactly as they were in this build")


class LatencyArgs(CommandArgs):
    count: int = Field(20, ge=1, le=50, description="how many times to send the command")


class SuiteArg(BaseModel):
    suite: str = Field(description="test suite name, e.g. 'smoke' or 'spec-calc'")


class SaveSuiteArgs(BaseModel):
    name: str = Field(description="suite name: lower-case letters, digits, '_' or '-'")
    description: str = Field("", max_length=300)
    project: Optional[str] = Field(None, description="the firmware project the suite tests")
    cases: List[TestCase] = Field(min_length=1, max_length=100)
    replace: bool = Field(False, description="replace the cases of an existing suite (not a built-in one)")


class AddCasesArgs(SuiteArg):
    cases: List[TestCase] = Field(min_length=1, max_length=50)


class RunSuiteArgs(SuiteArg):
    repeat: int = Field(1, ge=1, le=20)


class CompareArgs(BaseModel):
    baseline_run_id: int = Field(ge=1, description="the older test run")
    current_run_id: int = Field(ge=1, description="the newer test run")


class RegressionArgs(SuiteArg):
    build_id: Optional[int] = Field(None, ge=1, description="the build to check (default: newest build tested)")
    baseline_build: Optional[int] = Field(None, ge=1, description="compare with this build (default: the one before)")


class CoverageArgs(SuiteArg):
    project: Optional[str] = Field(None, description="compare with this project's specification")


FuzzFamily = Literal["boundary", "overflow", "mutation", "malformed", "oversize", "invalid", "state", "timeout"]


class FuzzArgs(BaseModel):
    project: Optional[str] = Field(None, description="use this project's specification and suites as the corpus")
    families: Optional[List[FuzzFamily]] = Field(None, description="which kinds of input (default: all)")
    seed: Optional[int] = Field(None, ge=0, description="the same seed gives the same inputs")
    max_inputs: int = Field(200, ge=1, le=500)
    max_seconds: int = Field(300, ge=1, le=600)
    max_resets: int = Field(3, ge=0, le=3, description="stop after the board restarted this often")


class CampaignArg(BaseModel):
    campaign_id: int = Field(ge=1)


class FindingsArgs(BaseModel):
    project: Optional[str] = None
    campaign_id: Optional[int] = Field(None, ge=1)
    status: Optional[Literal["open", "reproduced", "flaky", "not_reproduced", "fixed", "wont_fix"]] = None
    min_severity: Optional[Literal["critical", "high", "medium", "low", "info"]] = None


class FindingArg(BaseModel):
    finding_id: int = Field(ge=1)


class RemediateArgs(FindingArg):
    max_fix_rounds: int = Field(2, ge=0, le=4)


class RemediationArg(BaseModel):
    remediation_id: int = Field(ge=1)


class BenchNameArg(BaseModel):
    benchmark: str = Field(description="benchmark name, e.g. 'harness-basic' or 'bench-calc'")


class BenchCommandArg(BaseModel):
    name: str = Field(description="short name of the measurement")
    command: str = Field(description="the protocol line to measure, e.g. 'APP MUL 2 3'")
    repeat: int = Field(20, ge=1, le=100, description="sends per run")


class SaveBenchmarkArgs(BaseModel):
    name: str = Field(description="lower-case letters, digits, '_' or '-'")
    project: Optional[str] = None
    commands: List[BenchCommandArg] = Field(min_length=1, max_length=12)
    runs: int = Field(5, ge=2, le=20, description="repetitions of the whole list (for the statistics)")
    burst_command: Optional[str] = Field(None, description="command sent back-to-back to measure throughput")
    replace: bool = False


class BenchRunArg(BaseModel):
    run_id: int = Field(ge=1)


class CompareBenchArgs(BaseModel):
    before_run_id: int = Field(ge=1)
    after_run_id: int = Field(ge=1)


class VsBaselineArgs(BenchNameArg):
    run_id: Optional[int] = Field(None, ge=1, description="default: the latest run")


class OptimizeArgs(BaseModel):
    project: str
    goal: Literal["speed", "flash", "ram"] = "speed"
    target: Optional[str] = Field(None, description="the benchmark command to speed up (default: the slowest)")
    min_gain_pct: float = Field(10.0, ge=1, le=90)
    max_variants: int = Field(2, ge=1, le=3)


class OptimizationArg(BaseModel):
    optimization_id: int = Field(ge=1)


class KnowledgeSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=500, description="words to look for, e.g. 'division by zero hang'")
    kind: Optional[Literal["bug", "fix", "benchmark", "optimisation", "decision", "build_failure", "test_failure",
                           "spec", "note"]] = None
    project: Optional[str] = None


class KnowledgeIdArg(BaseModel):
    entry_id: int = Field(ge=1)


class TimelineArgs(BaseModel):
    project: str
    limit: int = Field(50, ge=1, le=200)


class DecisionArgs(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    context: str = Field(min_length=3, max_length=3000)
    decision: str = Field(min_length=3, max_length=3000)
    consequences: str = Field("", max_length=3000)
    project: Optional[str] = None


class NoteArgs(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=6000)
    project: Optional[str] = None


class ReproArgs(FindingArg):
    attempts: int = Field(3, ge=1, le=10)
    minimise: bool = Field(True, description="shrink the input to the smallest one that still fails")


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
           "INFO STATUS LED BLINK TIMER TEMP RESETINFO APP PROF. Cannot reboot, hang or flash the board.",
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

    T(Tool("security_scan", "Rule-based security and safety scan of app code (GPIO 12/13, USB stdio, unsafe string "
           "functions, flash/bootloader/watchdog access, heap, blocking loops, big stack arrays). Give project OR "
           "build_id. Verdict FAIL if any critical or high finding.",
           ScanArgs, "viewer", "read", lambda s, p, a: s.analysis.security_scan(a.project, a.build_id)))
    T(Tool("size_report", "Flash and RAM use of a successful build, its largest symbols, the app's own symbols and "
           "the change against the project's previous successful build.",
           BuildIdArg, "viewer", "read", lambda s, p, a: s.analysis.size_report(a.build_id)))
    T(Tool("measure_latency", "Send one safe protocol command several times and measure the round-trip time "
           "(min / mean / median / p95 / max in ms).",
           LatencyArgs, "agent", "write", lambda s, p, a: s.analysis.measure_latency(a.command, a.count, _who(p))))

    T(Tool("list_suites", "The stored test suites: name, project, number of cases, revision, built-in or not.",
           NoArgs, "viewer", "read", lambda s, p, a: s.tests.suites_db.list()))
    T(Tool("get_suite", "One test suite with all its cases.",
           SuiteArg, "viewer", "read", lambda s, p, a: s.tests.suites_db.get(a.suite)))
    T(Tool("save_suite", "Store a named test suite (cases: name, command, expect, optional wait_ms / retries) so it "
           "can be run again on every new build. Built-in suites cannot be changed.",
           SaveSuiteArgs, "agent", "write", _save_suite))
    T(Tool("add_test_cases", "Add cases to a stored (not built-in) suite.",
           AddCasesArgs, "agent", "write",
           lambda s, p, a: s.tests.suites_db.add_cases(a.suite, [c.model_dump(exclude_none=True) for c in a.cases])))
    T(Tool("run_suite", "Run a stored suite on the board as a job; the run records which build the board runs.",
           RunSuiteArgs, "agent", "write",
           lambda s, p, a: s.tests.start(RunRequest(suite=a.suite, repeat=a.repeat), _who(p))))
    T(Tool("compare_test_runs", "Compare two test runs case by case: regressed, fixed, still failing, added, removed.",
           CompareArgs, "viewer", "read", lambda s, p, a: s.tests.compare(a.baseline_run_id, a.current_run_id)))
    T(Tool("suite_regression", "Did a build break anything? The suite's latest run on that build against its latest "
           "run on the build before.",
           RegressionArgs, "viewer", "read",
           lambda s, p, a: s.tests.regression(a.suite, a.build_id, a.baseline_build)))
    T(Tool("suite_coverage", "What a suite covers: protocol commands, error codes, APP sub-commands, and the share of "
           "the project's specification examples.",
           CoverageArgs, "viewer", "read", lambda s, p, a: s.tests.coverage(a.suite, a.project)))

    T(Tool("start_fuzz", "Start a fuzz campaign against the board (a job): boundary, overflow, mutated, malformed, "
           "oversized and invalid input, state sequences and unfinished lines. The board may restart; budgets "
           "limit inputs, time and restarts. Never sends bootloader, hang or reboot commands.",
           FuzzArgs, "agent", "write",
           lambda s, p, a: s.fuzz.start(_who(p), a.project, a.families, a.seed, max_inputs=a.max_inputs,
                                        max_seconds=a.max_seconds, max_resets=a.max_resets)))
    T(Tool("get_fuzz_campaign", "A fuzz campaign: progress, budget, anomalies, restarts and its findings.",
           CampaignArg, "viewer", "read", lambda s, p, a: s.fuzz.campaign(a.campaign_id)))
    T(Tool("list_fuzz_findings", "Fuzz findings (most severe first): kind, severity, CWE class, input, reply, status.",
           FindingsArgs, "viewer", "read",
           lambda s, p, a: s.fuzz.store.findings(a.campaign_id, a.status, a.min_severity, a.project)))
    T(Tool("get_fuzz_finding", "One fuzz finding with its reproductions and minimised input.",
           FindingArg, "viewer", "read", lambda s, p, a: s.fuzz.finding(a.finding_id)))
    T(Tool("reproduce_finding", "Send a finding's input again (a job) to see whether it still fails on the build "
           "that is on the board, and shrink it to the smallest failing input.",
           ReproArgs, "agent", "write",
           lambda s, p, a: s.fuzz.start_reproduce(a.finding_id, _who(p), a.attempts, a.minimise)))
    T(Tool("start_remediation", "Fix a reproducible fuzz finding: reproduce, root cause + patch (Debugger agent), "
           "rebuild, scan, PROPOSE the deployment (a human approves), retest the bug, check for regressions, accept "
           "or roll back. Returns the remediation; it pauses at every deployment proposal.",
           RemediateArgs, "agent", "write",
           lambda s, p, a: s.remediations.start(a.finding_id, _who(p), a.max_fix_rounds)))
    T(Tool("get_remediation", "A remediation: stage, root cause, diff statistics, retest, regressions, outcome.",
           RemediationArg, "viewer", "read", lambda s, p, a: s.remediations.get(a.remediation_id)))

    T(Tool("list_benchmarks", "The stored benchmarks and their baselines.",
           NoArgs, "viewer", "read", lambda s, p, a: s.profiling.list()))
    T(Tool("save_benchmark", "Store a benchmark: commands to time on the board (each sent `repeat` times per run, "
           "`runs` runs), optionally a burst for throughput.",
           SaveBenchmarkArgs, "agent", "write", _save_benchmark))
    T(Tool("run_benchmark", "Run a benchmark on the board (a job): device-side microseconds from the harness profiler, "
           "host round trips, throughput, loop / busy / buffer figures, flash / RAM - with median, p95 and CV.",
           BenchNameArg, "agent", "write", lambda s, p, a: s.profiling.start(a.benchmark, _who(p))))
    T(Tool("get_benchmark_run", "One benchmark run with all its numbers.",
           BenchRunArg, "viewer", "read", lambda s, p, a: s.profiling.run(a.run_id)))
    T(Tool("compare_benchmark_runs", "Compare two benchmark runs: better / worse / same per measurement, only counting "
           "differences larger than the measured noise.",
           CompareBenchArgs, "viewer", "read",
           lambda s, p, a: s.profiling.compare(a.before_run_id, a.after_run_id)))
    T(Tool("benchmark_vs_baseline", "A run of a benchmark (default: the latest) against its baseline.",
           VsBaselineArgs, "viewer", "read", lambda s, p, a: s.profiling.against_baseline(a.benchmark, a.run_id)))
    T(Tool("benchmark_history", "Every run of a benchmark, newest first, with the build it ran on.",
           BenchNameArg, "viewer", "read", lambda s, p, a: s.profiling.history(a.benchmark)))
    T(Tool("start_optimization", "Optimise a project (it must be on the board): measure the baseline, the Performance "
           "agent proposes one change, the Firmware agent writes it, build + scan, PROPOSE the deployment (a human "
           "approves), A/B benchmark with noise margins, correctness suites + quick fuzz; keep it only if it is "
           "better by min_gain_pct and still correct, otherwise roll back (a human approves) and try another.",
           OptimizeArgs, "agent", "write",
           lambda s, p, a: s.optimizations.start(a.project, _who(p), a.goal, a.target, None, a.min_gain_pct,
                                                 a.max_variants)))
    T(Tool("get_optimization", "An optimisation: stage, variants tried and why they were rejected, A/B result.",
           OptimizationArg, "viewer", "read", lambda s, p, a: s.optimizations.get(a.optimization_id)))
    T(Tool("search_knowledge", "Search what the lab learned before: bugs (with their minimised inputs), fixes (root "
           "cause + diff), benchmarks, optimisations, failed builds and tests, decisions. Best matches first.",
           KnowledgeSearchArgs, "viewer", "read", lambda s, p, a: s.knowledge.search(a.query, a.kind, a.project)))
    T(Tool("get_knowledge", "One knowledge entry in full, with the record and build it came from.",
           KnowledgeIdArg, "viewer", "read", lambda s, p, a: s.knowledge.store.get(a.entry_id)))
    T(Tool("project_timeline", "A project's history, newest first: builds, deployments, tests, fuzzing, fixes, "
           "benchmarks, optimisations, pipelines, decisions.",
           TimelineArgs, "viewer", "read", lambda s, p, a: _timeline(s, a)))
    T(Tool("record_decision", "Write down an engineering decision (context, decision, consequences) so later tasks "
           "find it.", DecisionArgs, "agent", "write",
           lambda s, p, a: _decision(s, p, a)))
    T(Tool("add_knowledge_note", "Write down a lesson that the records do not show by themselves.",
           NoteArgs, "agent", "write", lambda s, p, a: _note(s, p, a)))
    return r


def _timeline(s, a):
    from .knowledge.history import timeline

    return timeline(s.settings.db_path, a.project, a.limit)


def _decision(s, p, a):
    from .knowledge.history import add_decision

    return add_decision(s.knowledge.store, a.title, a.context, a.decision, a.consequences, a.project, _who(p))


def _note(s, p, a):
    from .knowledge.history import add_note

    return add_note(s.knowledge.store, a.title, a.body, a.project, _who(p))


def _save_benchmark(s, p, a: SaveBenchmarkArgs):
    d = {"name": a.name, "project": a.project, "commands": [c.model_dump() for c in a.commands], "runs": a.runs}
    if a.burst_command:
        d["burst"] = {"command": a.burst_command, "count": 50}
    return s.profiling.save({k: v for k, v in d.items() if v is not None}, _who(p), a.replace)


def _save_suite(s, p, a: SaveSuiteArgs):
    return s.tests.suites_db.create(a.name, [c.model_dump(exclude_none=True) for c in a.cases], _who(p),
                                    a.description, a.project, source="agent", replace=a.replace)