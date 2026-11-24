"""Reproducing and minimising fuzz findings.

reproduce   send the finding's input again N times (after making sure the board answers) and count how often the
            same kind of anomaly comes back: reproduced (every time), flaky (sometimes), not_reproduced (never).
minimise    shrink the input while the anomaly still happens: first drop whole arguments, then cut the longest
            argument in halves, then single characters. Limited by a number of tries and by the reset budget,
            because every successful try of a hang or crash restarts the board.
The result is a reproduction recipe: the exact lines to send and what goes wrong, tied to the build it was seen on.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from .engine import Board, observe
from .generators import FuzzInput, is_safe
from .oracle import judge

RESETTING = {"hang", "crash", "dead"}
MINIMISABLE = {"hang", "crash", "dropped", "garbled", "id_mismatch", "bad_error", "oversized", "slow"}


def as_input(finding: dict, text: Optional[str] = None) -> FuzzInput:
    return FuzzInput(text if text is not None else finding["input_text"], finding["input_family"] or "replay",
                     finding.get("input_note") or "", finding["partial"], list(finding.get("sequence") or []))


def try_once(board: Board, finding: dict, text: Optional[str] = None, slow_ms: float = 500.0) -> Tuple[bool, dict]:
    """Send it once. (did the same kind of anomaly happen, what was seen)."""
    fi = as_input(finding, text)
    obs = observe(board, fi, board.uptime())
    kinds = [v.kind for v in judge(obs, slow_ms)]
    return finding["kind"] in kinds, {"text": fi.text, "reply": obs.reply, "error": obs.error, "kinds": kinds,
                                      "reset_reason": obs.reset_reason, "latency_ms": obs.latency_ms}


def reproduce(board: Board, finding: dict, attempts: int = 3) -> dict:
    tries: List[dict] = []
    for _ in range(attempts):
        if not board.recover():
            tries.append({"error": "the board does not answer before the attempt"})
            break
        hit, seen = try_once(board, finding)
        tries.append({"hit": hit, **seen})
    hits = sum(1 for t in tries if t.get("hit"))
    status = "reproduced" if hits == attempts else ("flaky" if hits else "not_reproduced")
    return {"status": status, "hits": hits, "attempts": len(tries), "tries": tries}


def _candidates(text: str) -> List[str]:
    words = text.split(" ")
    out = []
    keep = 2 if words[0].upper() == "APP" and len(words) > 1 else 1     # never drop the command itself
    for i in range(len(words) - 1, keep - 1, -1):                        # drop one argument
        out.append(" ".join(words[:i] + words[i + 1:]))
    longest = max(range(len(words)), key=lambda i: len(words[i]))
    w = words[longest]
    if len(w) > 1 and longest >= keep:                                   # halve the longest argument
        out.append(" ".join(words[:longest] + [w[: len(w) // 2]] + words[longest + 1:]))
        out.append(" ".join(words[:longest] + [w[: max(1, len(w) * 3 // 4)]] + words[longest + 1:]))
    if len(text) <= 40:
        for i in range(len(text) - 1, 0, -1):                            # drop one character
            out.append(text[:i] + text[i + 1:])
    return [c for c in out if c and c != text and is_safe(c)]


def minimise(board: Board, finding: dict, max_tries: int = 40, max_resets: int = 3,
             log: Callable[[str], None] = lambda t: None) -> dict:
    if finding["kind"] not in MINIMISABLE or finding["partial"] or finding.get("sequence"):
        return {"minimized": finding["input_text"], "tries": 0, "resets": 0, "note": "not minimised for this kind"}
    current, tries, resets = finding["input_text"], 0, 0
    improved = True
    while improved and tries < max_tries and resets <= max_resets:
        improved = False
        for cand in _candidates(current):
            if tries >= max_tries or resets > max_resets:
                break
            if not board.recover():
                return {"minimized": current, "tries": tries, "resets": resets,
                        "note": "the board stopped answering while minimising"}
            tries += 1
            hit, seen = try_once(board, finding, cand)
            if hit and finding["kind"] in RESETTING:
                resets += 1
            if hit:
                log(f"still fails with {cand!r}")
                current, improved = cand, True
                break
    note = "" if tries < max_tries and resets <= max_resets else "stopped by the try / reset budget"
    return {"minimized": current, "tries": tries, "resets": resets, "note": note,
            "shrunk_from": len(finding["input_text"]), "shrunk_to": len(current)}


def recipe(finding: dict, minimized: str, build: dict) -> dict:
    lines = list(finding.get("sequence") or [])[:-1] + [minimized]
    return {"send": lines, "partial": finding["partial"], "expect_anomaly": finding["kind"],
            "severity": finding["severity"], "seen_on_build": build.get("build_id"), "src": build.get("src_hash"),
            "then": "the board restarts by itself (watchdog)" if finding["kind"] == "hang" else ""}
