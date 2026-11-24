"""Firmware specifications: written by the model from a requirement (structured output), then checked here by
plain code. The examples in a spec become the functional tests of the pipeline, so they are fixed BEFORE any code
exists - the code is tested against the requirement, not against itself."""
from __future__ import annotations

import json
import re
from typing import List, Tuple

from ..build.engine import PROJECT_NAME_RE
from ..errors import ApiError
from ..services.device import validate_command

EXPECT_KINDS = ["equals", "prefix", "contains", "regex", "error"]
ERROR_CODES = {"1", "2", "3", "5", "6", "7", "8"}
MAX_COMMANDS = 12
MAX_EXAMPLES = 40

SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "commands": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string", "description": "the sub-command word, e.g. ADD"},
            "usage": {"type": "string", "description": "e.g. APP ADD <a> <b>"},
            "description": {"type": "string"},
            "reply": {"type": "string", "description": "reply text on success, e.g. sum=<a+b>"},
            "examples": {"type": "array", "items": {"type": "object", "properties": {
                "request": {"type": "string"},
                "expect": {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": EXPECT_KINDS},
                    "value": {"type": "string"}}, "required": ["kind", "value"]},
                "why": {"type": "string"}}, "required": ["request", "expect", "why"]}},
        }, "required": ["name", "usage", "description", "reply", "examples"]}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["project", "title", "summary", "commands", "constraints", "assumptions"],
}


class SpecError(ApiError):
    def __init__(self, problems: List[str]) -> None:
        super().__init__(422, "bad_spec", "the specification is not usable: " + "; ".join(problems[:5]), problems)
        self.problems = problems


def to_expect(e: dict) -> dict:
    kind, value = e["kind"], e["value"]
    if kind == "error":
        return {"prefix": f"ERR {value.strip()} "}
    return {kind: value}


def check_spec(spec: dict) -> Tuple[dict, List[str]]:
    """Returns (clean spec, problems). A spec with problems must not be used."""
    problems: List[str] = []
    project = str(spec.get("project", "")).strip().lower()
    if not PROJECT_NAME_RE.match(project):
        problems.append(f"project name {project!r} is not valid (lower-case, digits, '_', 2..32 chars)")
    commands = spec.get("commands") or []
    if not commands:
        problems.append("there are no commands")
    if len(commands) > MAX_COMMANDS:
        problems.append(f"more than {MAX_COMMANDS} commands")
    names, total = set(), 0
    for c in commands:
        name = str(c.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,23}", name):
            problems.append(f"command name {name!r} is not a plain word")
        if name.upper() in names:
            problems.append(f"command {name} is listed twice")
        names.add(name.upper())
        examples = c.get("examples") or []
        if len(examples) < 2:
            problems.append(f"command {name} needs at least two examples")
        for ex in examples:
            total += 1
            req = str(ex.get("request", ""))
            e = ex.get("expect") or {}
            if len(req) > 100 or not req.isascii() or not req.isprintable():
                problems.append(f"request {req[:40]!r} is too long or not printable ASCII")
                continue
            try:
                validate_command(req, operator=False)
            except ApiError as err:
                problems.append(f"request {req!r} is not allowed: {err.message}")
            if e.get("kind") not in EXPECT_KINDS or not isinstance(e.get("value"), str) or not e["value"]:
                problems.append(f"request {req!r} has no usable expectation")
            elif e["kind"] == "error" and e["value"].strip() not in ERROR_CODES:
                problems.append(f"request {req!r}: error code {e['value']!r} is not a protocol error code")
            elif e["kind"] == "regex":
                try:
                    re.compile(e["value"])
                except re.error as err:
                    problems.append(f"request {req!r}: bad regex ({err})")
    if total > MAX_EXAMPLES:
        problems.append(f"more than {MAX_EXAMPLES} examples")
    clean = {**spec, "project": project}
    return clean, problems


def spec_cases(spec: dict) -> List[dict]:
    """The spec's examples as test cases for the test runner, plus a harness check."""
    cases = [{"name": "harness still answers", "command": "PING", "expect": {"equals": "OK PONG"}}]
    seen = set()
    for c in spec["commands"]:
        for i, ex in enumerate(c["examples"], 1):
            name = f"{c['name']} #{i}"[:60]
            key = (ex["request"], ex["expect"]["kind"], ex["expect"]["value"])
            if key in seen:
                continue
            seen.add(key)
            cases.append({"name": name, "command": ex["request"], "expect": to_expect(ex["expect"])})
    return cases


def spec_for_coder(spec: dict) -> str:
    """The spec as the task text for the Firmware agent."""
    lines = [f"Implement the application firmware for project '{spec.get('project')}': "
             f"{spec.get('title') or spec.get('project')}.",
             spec.get("summary", ""), "", "Commands (every reply is written into `out`; the harness adds 'OK ' or "
                                          "the error):"]
    for c in spec.get("commands", []):
        lines.append(f"- {c.get('usage', c.get('name'))}: {c.get('description', '')} Success reply text: "
                     f"{c.get('reply', '')}")
        for ex in c.get("examples", []):
            e = ex.get("expect") or {}
            want = f"ERR {e.get('value')} ..." if e.get("kind") == "error" else f"{e.get('kind')} {e.get('value')!r}"
            lines.append(f"    {ex['request']!r} -> {want}   ({ex.get('why', '')})")
    if spec.get("constraints"):
        lines += ["", "Constraints:"] + [f"- {x}" for x in spec["constraints"]]
    if spec.get("assumptions"):
        lines += ["", "Assumptions already made:"] + [f"- {x}" for x in spec["assumptions"]]
    lines += ["", "Every example above is a test that will be run on the board. Make all of them pass."]
    return "\n".join(lines)


def write_spec(llm, requirement: str, platform_text: str, run_id=None, tries: int = 2) -> Tuple[dict, List[str], int]:
    """Ask the model for a spec; if plain-code checks find problems, give them back once and ask again.
    Returns (spec, problems, tokens used)."""
    from ..ai import prompts

    system = prompts.load("spec_writer").render(platform=platform_text)
    messages = [{"role": "user", "text": f"Requirement:\n{requirement}"}]
    problems: List[str] = []
    spec: dict = {}
    tokens = 0
    for attempt in range(tries):
        raw, reply = llm.generate_json(system, messages, SPEC_SCHEMA, purpose="pipeline:spec", run_id=run_id)
        tokens += reply.usage.total
        spec, problems = check_spec(raw)
        if not problems:
            return spec, [], tokens
        messages += [{"role": "model", "text": json.dumps(raw)},
                     {"role": "user", "text": "These problems were found; return the corrected specification:\n- "
                                              + "\n- ".join(problems)}]
    return spec, problems, tokens
