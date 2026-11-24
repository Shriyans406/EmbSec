"""The AEISP agent team: who exists, which prompt each one uses, which tools each one may call and what its report
looks like. A specialist's tool list is an allow-list: the graph refuses any other tool, even one the agent role
could call.

    supervisor   plans and delegates (tool: delegate + a few read-only tools)
    firmware     writes and fixes app code
    build        builds, proposes deployments, checks the running build
    security     reviews code (read-only)
    performance  size and latency (read-only + measuring)
    verification independent checks on the real board (cannot write code)
"""
from __future__ import annotations

from typing import Dict

from .graph import LAB_AGENT, REPORT_SCHEMA, AgentSpec

READ_CODE = ["list_projects", "list_sources", "read_source"]
READ_BUILDS = ["get_build", "build_history"]
JOBS = ["get_job", "get_job_logs", "wait_for_job"]
BENCH = ["list_benchmarks", "save_benchmark", "run_benchmark", "get_benchmark_run",
         "compare_benchmark_runs", "benchmark_vs_baseline", "benchmark_history"]
FUZZ = ["start_fuzz", "get_fuzz_campaign", "list_fuzz_findings", "get_fuzz_finding", "reproduce_finding"]


def _schema(extra: dict, required: list) -> dict:
    props = {"outcome": REPORT_SCHEMA["properties"]["outcome"], "summary": {"type": "string"},
             "facts": {"type": "array", "items": {"type": "string"}},
             "next_steps": {"type": "array", "items": {"type": "string"}}}
    props.update(extra)
    return {"type": "object", "properties": props, "required": ["outcome", "summary", "facts", "next_steps"] + required}


SECURITY_SCHEMA = _schema({
    "verdict": {"type": "string", "enum": ["pass", "fail"]},
    "findings": {"type": "array", "items": {"type": "object", "properties": {
        "rule": {"type": "string"}, "severity": {"type": "string"}, "file": {"type": "string"},
        "line": {"type": "integer"}, "real": {"type": "boolean"}, "risk": {"type": "string"},
        "fix": {"type": "string"}}, "required": ["severity", "file", "real", "risk", "fix"]}},
}, ["verdict", "findings"])

VERIFICATION_SCHEMA = _schema({
    "verdict": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
    "build_on_board": {"type": "integer", "description": "the build id VERSION reported, 0 if unknown"},
    "checks": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "command": {"type": "string"}, "expected": {"type": "string"},
        "observed": {"type": "string"}, "passed": {"type": "boolean"}},
        "required": ["name", "command", "expected", "observed", "passed"]}},
    "unverified": {"type": "array", "items": {"type": "string"}},
}, ["verdict", "build_on_board", "checks"])

PERFORMANCE_SCHEMA = _schema({
    "build_id": {"type": "integer"},
    "flash_bytes": {"type": "integer"},
    "ram_bytes": {"type": "integer"},
    "latency": {"type": "array", "items": {"type": "object", "properties": {
        "command": {"type": "string"}, "median_ms": {"type": "number"}, "p95_ms": {"type": "number"}},
        "required": ["command", "median_ms"]}},
    "improvements": {"type": "array", "items": {"type": "string"}},
}, ["build_id", "flash_bytes", "ram_bytes", "improvements"])

BUILD_SCHEMA = _schema({
    "build_id": {"type": "integer", "description": "0 if no build was made"},
    "build_status": {"type": "string"},
    "errors": {"type": "array", "items": {"type": "object", "properties": {
        "file": {"type": "string"}, "line": {"type": "integer"}, "message": {"type": "string"}},
        "required": ["message"]}},
    "proposal_job_id": {"type": "integer", "description": "0 if nothing was proposed"},
    "deployment": {"type": "string", "description": "not proposed / waiting / SUCCEEDED / REJECTED / FAILED ..."},
}, ["build_id", "build_status", "errors", "proposal_job_id", "deployment"])

FIRMWARE_SCHEMA = _schema({
    "files_written": {"type": "array", "items": {"type": "string"}},
    "commands": {"type": "array", "items": {"type": "object", "properties": {
        "request": {"type": "string"}, "reply": {"type": "string"}}, "required": ["request", "reply"]},
        "description": "every APP sub-command with an example request and its reply"},
    "scan_verdict": {"type": "string", "description": "PASS / FAIL of the last security_scan, or 'not run'"},
}, ["files_written", "commands", "scan_verdict"])

DEBUG_SCHEMA = _schema({
    "root_cause": {"type": "string", "description": "file, line and mechanism of the bug"},
    "fix": {"type": "string", "description": "what was changed and why it removes the cause"},
    "files_written": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}, ["root_cause", "fix", "files_written", "confidence"])

SUPERVISOR = AgentSpec(
    "supervisor", "supervisor",
    ["list_projects", "list_sources", "build_history", "get_build", "deployment_history", "list_test_runs",
     "device_status", "list_fuzz_findings", "get_remediation", "get_optimization"],
    "plans the work and delegates to the specialists", max_steps=30, extra={"delegates": True})

TEAM: Dict[str, AgentSpec] = {s.name: s for s in [
    SUPERVISOR,
    AgentSpec("firmware", "firmware_agent",
              READ_CODE + ["create_project", "write_source", "delete_source", "restore_sources", "security_scan"]
              + READ_BUILDS,
              "writes and fixes application code", max_steps=16,
              report_prompt="specialist_report", report_schema=FIRMWARE_SCHEMA),
    AgentSpec("build", "build_agent",
              ["list_sources", "read_source", "start_build"] + JOBS + READ_BUILDS
              + ["propose_deployment", "propose_recovery", "deployment_history", "known_good_info", "device_status",
                 "device_command"],
              "builds, proposes deployments and checks the running build", max_steps=12,
              report_prompt="specialist_report", report_schema=BUILD_SCHEMA),
    AgentSpec("security", "security_agent", READ_CODE + READ_BUILDS + ["security_scan"] + FUZZ + JOBS,
              "reviews code and fuzzes the board for security problems (cannot change code)", max_steps=14,
              report_prompt="specialist_report", report_schema=SECURITY_SCHEMA),
    AgentSpec("performance", "performance_agent",
              ["list_sources", "read_source"] + READ_BUILDS + ["size_report", "measure_latency", "device_status",
                                                               "device_command"] + BENCH + JOBS,
              "measures size, speed and load on the board (cannot change code)", max_steps=14,
              report_prompt="specialist_report", report_schema=PERFORMANCE_SCHEMA),
    AgentSpec("debugger", "debug_agent",
              READ_CODE + ["write_source", "security_scan", "get_fuzz_finding", "list_fuzz_findings"] + READ_BUILDS,
              "finds the root cause of a reproduced bug and patches it", max_steps=16,
              report_prompt="specialist_report", report_schema=DEBUG_SCHEMA),
    AgentSpec("verification", "verification_agent",
              ["device_status", "device_command", "run_tests", "get_test_run", "list_test_runs"] + JOBS
              + READ_BUILDS + ["deployment_history", "list_suites", "get_suite", "save_suite", "add_test_cases",
                               "run_suite", "compare_test_runs", "suite_regression", "suite_coverage"],
              "independently checks behaviour on the board (cannot change code)", max_steps=14,
              report_prompt="specialist_report", report_schema=VERIFICATION_SCHEMA),
]}

SPECIALISTS = [n for n in TEAM if n != "supervisor"]
WRITERS = {"firmware", "debugger"}      # the only agents that may change code


def all_specs() -> Dict[str, AgentSpec]:
    return {LAB_AGENT.name: LAB_AGENT, **TEAM}
