"""Coverage of a test suite: which protocol commands, error codes and APP sub-commands it exercises, how many of
its checks expect success vs. failure, and - when the project has a specification from the pipeline - how many of
the spec's commands and examples the suite covers. Measured from the suite's cases and its latest run."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..services.device import OPERATOR_VERBS, SAFE_VERBS

ERROR_NAMES = {1: "UNKNOWN_CMD", 2: "BAD_ARGS", 3: "BAD_RANGE", 4: "BAD_CRC", 5: "TOO_LONG", 6: "BAD_FORMAT",
               7: "TIMEOUT", 8: "BAD_STATE"}
_ERR = re.compile(r"^\^?ERR (\d)")


def _pct(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def expected_error(expect: dict) -> Optional[int]:
    for key in ("equals", "prefix", "regex"):
        m = _ERR.match(expect.get(key) or "")
        if m:
            return int(m.group(1))
    return None


def expects_success(expect: dict) -> Optional[bool]:
    if expected_error(expect) is not None:
        return False
    for key in ("equals", "prefix"):
        if (expect.get(key) or "").startswith("OK"):
            return True
    return None                                       # cannot tell (e.g. only a field or latency check)


def suite_coverage(cases: List[dict], last_run: Optional[dict] = None, spec: Optional[dict] = None) -> dict:
    enabled = [c for c in cases if c.get("enabled", True)]
    verbs_all = sorted(SAFE_VERBS | OPERATOR_VERBS)
    used_verbs: Dict[str, int] = {}
    app_subs: Dict[str, int] = {}
    errors_expected: Dict[int, int] = {}
    positive = negative = unknown = 0
    for c in enabled:
        words = c["command"].split()
        verb = words[0].upper() if words else ""
        used_verbs[verb] = used_verbs.get(verb, 0) + 1
        if verb == "APP" and len(words) > 1:
            app_subs[words[1].upper()] = app_subs.get(words[1].upper(), 0) + 1
        code = expected_error(c["expect"])
        if code is not None:
            errors_expected[code] = errors_expected.get(code, 0) + 1
        kind = expects_success(c["expect"])
        positive += kind is True
        negative += kind is False
        unknown += kind is None
    out = {
        "cases": len(cases), "enabled": len(enabled),
        "protocol": {"covered": sorted(v for v in used_verbs if v in verbs_all),
                     "missing": [v for v in verbs_all if v not in used_verbs],
                     "percent": _pct(len([v for v in used_verbs if v in verbs_all]), len(verbs_all))},
        "error_codes": {"expected": {ERROR_NAMES.get(k, str(k)): n for k, n in sorted(errors_expected.items())},
                        "missing": [ERROR_NAMES[k] for k in (1, 2, 3, 8) if k not in errors_expected]},
        "app_subcommands": app_subs,
        "balance": {"expect_success": positive, "expect_error": negative, "other": unknown},
        "last_run": None, "spec": None,
    }
    if last_run:
        results = (last_run.get("result") or {}).get("cases", [])
        ran = {r["name"] for r in results}
        observed = sorted({int(m.group(1)) for r in results for m in [_ERR.match(r.get("reply") or "")] if m})
        out["last_run"] = {"run_id": last_run["id"], "build_id": last_run.get("build_id"),
                           "passed": last_run["passed"], "failed": last_run["failed"],
                           "not_run": [c["name"] for c in enabled if c["name"] not in ran],
                           "error_codes_seen": [ERROR_NAMES.get(k, str(k)) for k in observed]}
    if spec:
        spec_cmds = [c["name"].upper() for c in spec.get("commands", [])]
        requests = {c["command"] for c in enabled}
        examples = [(c["name"], ex["request"]) for c in spec.get("commands", []) for ex in c.get("examples", [])]
        missing = [{"command": n, "request": r} for n, r in examples if r not in requests]
        out["spec"] = {"commands": spec_cmds,
                       "commands_covered": [n for n in spec_cmds if n in app_subs],
                       "commands_missing": [n for n in spec_cmds if n not in app_subs],
                       "examples": len(examples), "examples_covered": len(examples) - len(missing),
                       "examples_missing": missing[:30],
                       "percent": _pct(len(examples) - len(missing), len(examples))}
    return out
