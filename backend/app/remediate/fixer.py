"""The fix stage's helpers: the task text for the Debugger agent, and the code difference of a fix."""
from __future__ import annotations

import difflib
import json
from typing import Dict, Optional


def debug_task(evidence: dict, project: str, before_build: int, retry_note: Optional[str] = None) -> str:
    f = evidence["finding"]
    lines = [f"Project '{project}'. The board runs build #{before_build} and has this bug (reproduced on the board):",
             f"- kind: {f['kind']} ({f['severity']}, {f['cwe']})",
             f"- what happened: {f['detail']}",
             f"- minimised input: {(f['minimized'] or f['input_text'])!r}",
             f"- original input: {f['input_text']!r}" + (f"  ({f['input_note']})" if f.get("input_note") else ""),
             f"- reply: {f['reply']!r}" if f.get("reply") else "- reply: none (the board stopped answering)"]
    recipe = evidence.get("recipe")
    if recipe:
        lines.append(f"- reproduction: send {recipe['send']}; expected anomaly: {recipe['expect_anomaly']}")
    lines.append(f"Source files: {', '.join(evidence['files'])}.")
    if evidence.get("scan", {}).get("findings"):
        lines.append("Security scan notes: " + "; ".join(f"{x['file']}:{x['line']} {x['title']}"
                                                         for x in evidence["scan"]["findings"][:5]))
    if retry_note:
        lines += ["", "YOUR PREVIOUS FIX DID NOT WORK:", retry_note]
    lines += ["", "Find the root cause, fix it with the smallest correct change, and keep every other behaviour."]
    return "\n".join(lines)


def code_diff(before: Dict[str, str], after: Dict[str, str]) -> dict:
    """Unified diff of the app sources, and how much changed."""
    parts, added, removed, changed = [], 0, 0, []
    for name in sorted(set(before) | set(after)):
        a = before.get(name, "").splitlines(keepends=True)
        b = after.get(name, "").splitlines(keepends=True)
        if a == b:
            continue
        changed.append(name)
        d = list(difflib.unified_diff(a, b, fromfile=f"a/{name}" if name in before else "/dev/null",
                                      tofile=f"b/{name}" if name in after else "/dev/null", n=3))
        added += sum(1 for x in d if x.startswith("+") and not x.startswith("+++"))
        removed += sum(1 for x in d if x.startswith("-") and not x.startswith("---"))
        parts.append("".join(d) + ("" if (d and d[-1].endswith("\n")) else "\n"))
    return {"diff": "".join(parts), "files": changed, "added": added, "removed": removed}


def root_cause_of(result: dict) -> str:
    rep = result.get("report") or {}
    text = rep.get("root_cause") or ""
    if not text:
        summary = rep.get("summary") or ""
        i = summary.find("Root cause:")
        text = summary[i:].split("Fix:")[0].strip() if i >= 0 else summary
    return text[:2000]


def short(obj) -> str:
    return json.dumps(obj, default=str)[:300]
