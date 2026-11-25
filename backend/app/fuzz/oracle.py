"""The fuzz oracle: decides whether what the board did after an input is abnormal, and how bad it is.

The board can only show trouble in a few ways, and each is checked with plain rules:
    dead        it never came back (critical: a human has to look)
    hang        no reply, then a watchdog reset                          (the input locked the firmware up)
    crash       the link dropped or the board restarted without a watchdog timeout, or its uptime went back
    dropped     no reply, but the board is alive and did not restart      (the input was silently swallowed)
    garbled     a reply that is not "OK ..." / "ERR ...", non-printable characters, or a broken checksum
    id_mismatch the reply does not carry the request id it was asked with
    bad_error   ERR with a code / name the protocol does not define
    oversized   a reply longer than the protocol allows
    no_timeout  an unfinished line was never answered with ERR 7 TIMEOUT
    slow        the reply took longer than the limit
    accepted    an out-of-range or malformed number was accepted with OK (informational)
    out_of_range the project's specification limits a command's numbers (e.g. 32-bit signed) and a number outside
                that range was answered with OK instead of an error (rules from spec_rules)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from ..hardware.protocol import ERROR_NAMES, crc16

SEVERITY = {"dead": "critical", "hang": "high", "crash": "high", "dropped": "medium", "garbled": "medium",
            "id_mismatch": "medium", "out_of_range": "medium", "bad_error": "low", "oversized": "low", "no_timeout": "low", "slow": "low",
            "accepted": "info"}
CWE = {"dead": "CWE-400 uncontrolled resource consumption / denial of service",
       "hang": "CWE-835 loop with unreachable exit (denial of service)",
       "crash": "CWE-787/CWE-120 out-of-bounds write or unhandled fault suspected",
       "dropped": "CWE-703 improper handling of exceptional conditions",
       "garbled": "CWE-116 improper encoding of output",
       "id_mismatch": "CWE-436 interpretation conflict (reply cannot be matched to its request)",
       "bad_error": "CWE-393 return of wrong status code",
       "oversized": "CWE-130 improper handling of length",
       "no_timeout": "CWE-400 resource held by an unfinished request",
       "slow": "CWE-407 inefficient algorithmic complexity",
       "accepted": "CWE-20 improper input validation",
       "out_of_range": "CWE-20 improper input validation (a number outside the specified range was accepted)"}
RANK = ["critical", "high", "medium", "low", "info"]
REPLY_MAX = 256
_ID = re.compile(r"^#(\d{1,5}) ")


@dataclass
class Observation:
    text: str
    family: str
    reply: Optional[str] = None
    error: str = ""                         # "timeout" | "link_lost" | "" ...
    latency_ms: float = 0.0
    alive_after: Optional[bool] = None      # did PING answer after the input
    reset_reason: Optional[str] = None      # RESETINFO after an anomaly
    uptime_before: Optional[int] = None
    uptime_after: Optional[int] = None
    partial_replies: List[str] = field(default_factory=list)
    partial: bool = False
    note: str = ""
    sequence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    kind: str
    severity: str
    detail: str
    cwe: str

    def to_dict(self) -> dict:
        return asdict(self)


def _v(kind: str, detail: str) -> Verdict:
    return Verdict(kind, SEVERITY[kind], detail, CWE[kind])


def request_id(text: str) -> Optional[int]:
    m = _ID.match(text)
    return int(m.group(1)) if m else None


INT32 = (-(2 ** 31), 2 ** 31 - 1)
_NUM = re.compile(r"^[+-]?\d+$")


def spec_rules(spec: Optional[dict]) -> dict:
    """Number ranges per APP command from a project's specification: {"APP ADD": (lo, hi), ...}.
    A command gets the 32-bit signed range when the spec says "32-bit", or when one of its error examples uses a
    number outside that range."""
    rules: dict = {}
    if not spec:
        return rules
    text = " ".join([spec.get("summary", "") or "", " ".join(spec.get("constraints", []) or [])]).lower()
    whole = "32-bit" in text or "32 bit" in text or "int32" in text
    for c in spec.get("commands", []):
        words = (c.get("usage") or "").split()
        if len(words) < 3 or words[0].upper() != "APP":
            continue
        own = " ".join([c.get("description", "") or "", c.get("usage", "") or ""]).lower()
        big = any((ex.get("expect") or {}).get("kind") == "error" and any(
            _NUM.match(w) and not INT32[0] <= int(w) <= INT32[1] for w in ex.get("request", "").split()[2:])
            for ex in c.get("examples", []))
        if whole or big or "32-bit" in own:
            rules[f"APP {words[1].upper()}"] = INT32
    return rules


def judge(obs: Observation, slow_ms: float = 500.0, rules: Optional[dict] = None) -> List[Verdict]:
    out: List[Verdict] = []
    restarted = obs.uptime_before is not None and obs.uptime_after is not None and obs.uptime_after < obs.uptime_before
    if obs.partial:
        if obs.alive_after is False:
            return [_v("dead", "the board stopped answering after an unfinished line")]
        if not any(r.startswith("ERR 7") for r in obs.partial_replies):
            out.append(_v("no_timeout", f"an unfinished line got {obs.partial_replies or 'no reply'} instead of "
                                        "ERR 7 TIMEOUT"))
        return out
    if obs.reply is None:
        if obs.alive_after and not restarted and obs.text.strip(" ") == "" and not obs.reset_reason:
            return []                     # the harness ignores blank lines on purpose: no reply is correct
        if obs.alive_after is False:
            return [_v("dead", f"no reply ({obs.error or 'nothing'}) and the board did not come back")]
        if obs.reset_reason == "WATCHDOG_TIMEOUT":
            return [_v("hang", "no reply, then the watchdog reset the board (the input locked it up)")]
        if restarted or obs.error == "link_lost" or (obs.reset_reason and obs.reset_reason != "POWER_ON_OR_RESET"):
            return [_v("crash", f"no reply and the board restarted (reset reason {obs.reset_reason})")]
        return [_v("dropped", f"no reply ({obs.error or 'timeout'}) but the board kept running")]
    reply = obs.reply
    if restarted:
        out.append(_v("crash", f"the board restarted while answering (uptime {obs.uptime_before} -> "
                               f"{obs.uptime_after} ms, reset reason {obs.reset_reason})"))
    body = reply
    m = re.search(r"\*([0-9A-Fa-f]{4})$", body)
    if m:
        if crc16(body[: m.start()]) != int(m.group(1), 16):
            out.append(_v("garbled", f"the reply's checksum is wrong: {reply[:80]!r}"))
        body = body[: m.start()]
    if not reply.isprintable():
        out.append(_v("garbled", f"the reply has non-printable characters: {reply[:80]!r}"))
    rid = request_id(obs.text)
    got = request_id(body)
    if rid is not None and got != rid and not re.match(r"^(#\d+ )?ERR (5|6)", body):
        out.append(_v("id_mismatch", f"asked with #{rid}, answered with {('#' + str(got)) if got is not None else 'no id'}"))
    core = _ID.sub("", body, count=1)
    if not (core == "OK" or core.startswith("OK ") or core.startswith("ERR ")):
        out.append(_v("garbled", f"the reply is neither OK nor ERR: {reply[:80]!r}"))
    elif core.startswith("ERR "):
        parts = core.split(" ")
        try:
            code = int(parts[1])
        except (IndexError, ValueError):
            code = None
        name = parts[2] if len(parts) > 2 else ""
        if code not in ERROR_NAMES or ERROR_NAMES[code] != name:
            out.append(_v("bad_error", f"undefined error {parts[1:3]} in {reply[:80]!r}"))
    if len(reply) > REPLY_MAX:
        out.append(_v("oversized", f"the reply is {len(reply)} characters long"))
    if obs.latency_ms > slow_ms:
        out.append(_v("slow", f"the reply took {obs.latency_ms:.0f} ms (limit {slow_ms:.0f} ms)"))
    words = _ID.sub("", obs.text).split()
    key = " ".join(words[:2]).upper() if len(words) > 1 and words[0].upper() == "APP" else ""
    rng = (rules or {}).get(key)
    outside = [w for w in words[2:] if _NUM.match(w) and rng and not rng[0] <= int(w) <= rng[1]]
    if core.startswith("OK") and outside:
        out.append(_v("out_of_range", f"{outside[0][:24]} is outside the specified range {rng[0]}..{rng[1]} of "
                                      f"{key}, but the reply was {reply[:60]!r} instead of an error"))
    elif obs.family == "overflow" and core.startswith("OK") and obs.text.upper().startswith("APP "):
        out.append(_v("accepted", f"{obs.text!r} was accepted: {reply[:80]!r}"))
    return out


def signature(text: str, kind: str) -> str:
    """Findings with the same kind on the same command count as one finding."""
    words = _ID.sub("", text).split()
    head = words[0].upper() if words else "<empty>"
    if head == "APP" and len(words) > 1:
        head += " " + words[1].upper()
    return f"{kind}:{head}"


def worst(verdicts: List[Verdict]) -> Optional[Verdict]:
    return min(verdicts, key=lambda v: RANK.index(v.severity)) if verdicts else None
