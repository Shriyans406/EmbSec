"""Fuzz campaigns: generate inputs, send them to the board one by one, watch what happens, record findings.

Every campaign has hard limits, checked before each input:
    max_inputs   how many inputs at most
    max_seconds  how long at most
    max_resets   how many times the board may restart (hang -> watchdog, crash) before the campaign stops
A board that does not come back stops the campaign at once (finding "dead"). Nothing outside the safety filter
(no bootloader, hang or reboot command) is ever sent. After every input the board's uptime is read, so a restart
is noticed even when the reply looked normal.
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Callable, List, Optional

from ..db import now_iso
from ..errors import ApiError
from ..hardware.link import LinkError, LinkTimeout
from ..jobs import JobContext, JobFailed
from .generators import FAMILIES, FuzzInput, Generator, command_shapes, corpus_from, is_safe
from .oracle import Observation, judge, signature, worst
from .store import FuzzStore

log = logging.getLogger("aeisp.fuzz")

AGENT_LIMITS = {"max_inputs": 500, "max_seconds": 600, "max_resets": 3}
HARD_LIMITS = {"max_inputs": 2000, "max_seconds": 1800, "max_resets": 10}
DEFAULT_BUDGET = {"max_inputs": 200, "max_seconds": 300, "max_resets": 3, "slow_ms": 500, "recover_s": 15.0}


class Board:
    """The few things the fuzzer needs from a board session: send, probe, recover."""

    def __init__(self, send: Callable[[str], str], recover_s: float = 15.0, sleep=time.sleep) -> None:
        self.send = send
        self.partial = getattr(send, "partial", None)
        self.recover_s = recover_s
        self.sleep = sleep

    def call(self, text: str):
        """(reply or None, error kind, latency ms)."""
        t0 = time.perf_counter()
        try:
            reply = self.send(text)
            return reply, "", (time.perf_counter() - t0) * 1000
        except LinkTimeout:
            return None, "timeout", (time.perf_counter() - t0) * 1000
        except LinkError:
            return None, "link_lost", (time.perf_counter() - t0) * 1000

    def uptime(self) -> Optional[int]:
        reply, _, _ = self.call("UPTIME")
        try:
            return int(reply.split()[-1]) if reply and reply.startswith("OK UPTIME") else None
        except ValueError:
            return None

    def recover(self) -> bool:
        """Wait (up to recover_s) for the board to answer PING again, e.g. after a watchdog reset."""
        deadline = time.monotonic() + self.recover_s
        while True:
            reply, _, _ = self.call("PING")
            if reply == "OK PONG":
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(0.5)

    def reset_reason(self) -> Optional[str]:
        reply, _, _ = self.call("RESETINFO")
        if reply and "reason=" in reply:
            return reply.split("reason=", 1)[1].split()[0]
        return None

    def firmware(self) -> dict:
        reply, _, _ = self.call("VERSION")
        fields = dict(kv.split("=", 1) for kv in (reply or "").split() if "=" in kv)
        b = fields.get("build")
        return {"fw_version": fields.get("fw"), "build_id": int(b) if b and b.isdigit() else None,
                "src_hash": fields.get("src")}


def observe(board: Board, fi: FuzzInput, last_uptime: Optional[int]) -> Observation:
    """Send one input (or its whole sequence) and collect everything the oracle needs."""
    obs = Observation(text=fi.text, family=fi.family, partial=fi.partial, uptime_before=last_uptime, note=fi.note,
                      sequence=list(fi.sequence))
    if fi.partial:
        if board.partial is None:
            obs.partial_replies = ["ERR 7 (partial lines not supported by this link; skipped)"]
            obs.alive_after = True
            return obs
        try:
            obs.partial_replies = board.partial(fi.text, 3.0)
        except LinkError:
            obs.partial_replies = []
        obs.alive_after = board.recover()
        return obs
    for line in fi.sequence[:-1]:                # the lead-up of a state sequence
        reply, err, _ = board.call(line)
        if reply is None:
            obs.text, obs.error = line, err
            break
    else:
        obs.reply, obs.error, obs.latency_ms = board.call(fi.text)
    obs.latency_ms = round(obs.latency_ms, 1)
    up = board.uptime()
    if up is None:                               # no answer to UPTIME: wait for the board to come back
        obs.alive_after = board.recover()
        up = board.uptime() if obs.alive_after else None
        if obs.alive_after:
            obs.reset_reason = board.reset_reason()
    else:
        obs.alive_after = True
    obs.uptime_after = up
    if up is not None and last_uptime is not None and up < last_uptime and obs.reset_reason is None:
        obs.reset_reason = board.reset_reason()
    return obs


class FuzzService:
    def __init__(self, db_path, jobs, device, tests=None, pipelines=None) -> None:
        self.store = FuzzStore(db_path)
        self.jobs = jobs
        self.device = device
        self.tests = tests
        self.pipelines = pipelines
        self.sleep = time.sleep
        self.recover_s: Optional[float] = None    # tests make the recovery wait short
        jobs.register("fuzz", self._run, "board")
        jobs.register("fuzz_repro", self._reproduce, "board")

    def recover(self) -> int:
        return self.store.recover_orphans()

    # ------------------------------------------------------------ starting
    def budget(self, given: dict, elevated: bool) -> dict:
        b = {**DEFAULT_BUDGET, **{k: v for k, v in given.items() if v is not None}}
        if self.recover_s is not None:
            b["recover_s"] = self.recover_s
        caps = HARD_LIMITS if elevated else AGENT_LIMITS
        for key, cap in caps.items():
            if not 1 <= int(b[key]) <= cap:
                who = "anyone" if elevated else "the agent role (an operator may allow more)"
                raise ApiError(422, "budget_too_large", f"{key} must be 1..{cap} for {who}")
        return b

    def _context(self, project: Optional[str]):
        spec = self.tests.latest_spec(project) if (self.tests and project) else None
        suites = []
        if self.tests and project:
            suites = [self.tests.suites_db.get(s["name"]) for s in self.tests.suites_db.list(project)]
        return spec, suites

    def start(self, who: str, project: Optional[str] = None, families: Optional[List[str]] = None,
              seed: Optional[int] = None, elevated: bool = False, **budget) -> dict:
        fams = families or list(FAMILIES)
        bad = [f for f in fams if f not in FAMILIES]
        if bad:
            raise ApiError(422, "bad_family", f"unknown families {bad}; known: {FAMILIES}")
        b = self.budget(budget, elevated)
        seed = seed if seed is not None else random.randrange(1, 2 ** 31)
        camp = self.store.create(project, seed, fams, b, who)
        job = self.jobs.submit("fuzz", {"campaign_id": camp["id"]}, who,
                               summary=f"fuzz campaign {camp['id']} ({b['max_inputs']} inputs max, seed {seed})")
        self.store.update(camp["id"], job_id=job["id"])
        return self.store.get(camp["id"])

    def stop(self, cid: int) -> dict:
        camp = self.store.get(cid)
        if camp["status"] not in ("QUEUED", "RUNNING"):
            raise ApiError(409, "campaign_finished", f"campaign {cid} is {camp['status']}")
        self.store.update(cid, stop_requested=1)
        return self.store.get(cid)

    def rules_for(self, project: Optional[str]) -> dict:
        """What the project's specification says about number ranges (for the oracle)."""
        from .oracle import spec_rules

        return spec_rules(self.tests.latest_spec(project)) if (self.tests and project) else {}

    def inputs_for(self, camp: dict) -> List[FuzzInput]:
        spec, suites = self._context(camp["project"])
        gen = Generator(camp["seed"], corpus_from(spec, suites), command_shapes(spec))
        return gen.generate(camp["families"], camp["budget"]["max_inputs"])

    # ------------------------------------------------------------ running
    def _run(self, ctx: JobContext) -> dict:
        camp = self.store.get(ctx.params["campaign_id"])
        b = camp["budget"]
        inputs = self.inputs_for(camp)
        rules = self.rules_for(camp["project"])
        assert all(is_safe(fi.text) for fi in inputs)           # the filter is applied twice, on purpose
        self.store.update(camp["id"], status="RUNNING", started_at=now_iso(), inputs_planned=len(inputs))
        ctx.log(f"campaign {camp['id']}: {len(inputs)} inputs, seed {camp['seed']}, families {camp['families']}")
        sent = anomalies = resets = 0
        stop = ""
        by_kind: dict = {}
        by_family: dict = {}
        new_findings: List[int] = []
        t_end = time.monotonic() + b["max_seconds"]
        with self.device.session(f"fuzz campaign #{camp['id']}") as send:
            board = Board(send, b["recover_s"], self.sleep)
            if not board.recover():
                self.store.update(camp["id"], status="FAILED", stop_reason="the board does not answer PING",
                                  finished_at=now_iso())
                raise JobFailed("the board does not answer PING; nothing was sent")
            fw = board.firmware()
            camp = {**camp, **fw}
            self.store.update(camp["id"], **fw)
            ctx.log(f"board runs build #{fw.get('build_id')} ({fw.get('fw_version')})")
            uptime = board.uptime()
            for fi in inputs:
                if self.store.stop_requested(camp["id"]):
                    stop = "stopped by request"
                    break
                if time.monotonic() > t_end:
                    stop = f"time budget of {b['max_seconds']} s used up"
                    break
                obs = observe(board, fi, uptime)
                sent += 1
                by_family[fi.family] = by_family.get(fi.family, 0) + 1
                verdicts = judge(obs, b["slow_ms"], rules)
                restarted = (obs.uptime_after is not None and uptime is not None and obs.uptime_after < uptime) \
                    or any(v.kind in ("hang", "crash") for v in verdicts)
                uptime = obs.uptime_after if obs.uptime_after is not None else uptime
                for v in verdicts:
                    by_kind[v.kind] = by_kind.get(v.kind, 0) + 1
                    fid, new = self.store.record(camp, signature(obs.text, v.kind), v, obs, camp["project"])
                    if new:
                        new_findings.append(fid)
                        ctx.log(f"FINDING #{fid} [{v.severity}] {v.kind}: {obs.text[:60]!r} - {v.detail}")
                if verdicts and worst(verdicts).severity != "info":
                    anomalies += 1
                if restarted:
                    resets += 1
                if any(v.kind == "dead" for v in verdicts):
                    stop = "the board did not come back; a human has to look at it"
                    break
                if resets > b["max_resets"]:
                    stop = f"reset budget of {b['max_resets']} used up"
                    break
                if sent % 25 == 0:
                    ctx.log(f"{sent}/{len(inputs)} sent, {anomalies} anomalies, {resets} resets")
                    self.store.update(camp["id"], inputs_sent=sent, anomalies=anomalies, resets=resets)
        summary = {"by_family": by_family, "by_kind": by_kind, "new_findings": new_findings}
        status = "STOPPED" if stop and "request" in stop else "FINISHED"
        self.store.update(camp["id"], status=status, inputs_sent=sent, anomalies=anomalies, resets=resets,
                          findings=len(new_findings), stop_reason=stop or None, summary_json=json.dumps(summary),
                          finished_at=now_iso())
        ctx.log(f"campaign {camp['id']} {status}: {sent} inputs, {anomalies} anomalies, {resets} resets, "
                f"{len(new_findings)} findings" + (f" ({stop})" if stop else ""))
        out = {"campaign_id": camp["id"], "status": status, "inputs_sent": sent, "anomalies": anomalies,
               "resets": resets, "findings": new_findings, "stop_reason": stop or None}
        if stop and "did not come back" in stop:
            raise JobFailed(stop, out)
        return out

    # ------------------------------------------------------------ reproduction
    def start_reproduce(self, fid: int, who: str, attempts: int = 3, minimise: bool = True,
                        max_tries: int = 40, max_resets: int = 3) -> dict:
        f = self.store.finding(fid)
        if not 1 <= attempts <= 10 or not 0 <= max_tries <= 100 or not 0 <= max_resets <= AGENT_LIMITS["max_resets"]:
            raise ApiError(422, "budget_too_large", "attempts 1..10, max_tries 0..100, max_resets 0..3")
        return self.jobs.submit("fuzz_repro", {"finding_id": fid, "attempts": attempts, "minimise": minimise,
                                               "max_tries": max_tries, "max_resets": max_resets}, who,
                                summary=f"reproduce fuzz finding #{fid} ({f['kind']}: {f['input_text'][:40]!r})")

    def _reproduce(self, ctx: JobContext) -> dict:
        from .repro import minimise, recipe, reproduce

        p = ctx.params
        f = self.store.finding(p["finding_id"])
        with self.device.session(f"reproduce finding #{f['id']}") as send:
            board = Board(send, self.recover_s or DEFAULT_BUDGET["recover_s"], self.sleep)
            if not board.recover():
                raise JobFailed("the board does not answer PING")
            fw = board.firmware()
            rules = self.rules_for(f["project"])
            rep = reproduce(board, f, p["attempts"], rules)
            ctx.log(f"finding #{f['id']}: {rep['status']} ({rep['hits']}/{rep['attempts']}) on build "
                    f"#{fw.get('build_id')}")
            mini = {"minimized": f["input_text"], "tries": 0, "resets": 0, "note": "not reproduced"}
            if rep["status"] == "reproduced" and p["minimise"]:
                mini = minimise(board, f, p["max_tries"], p["max_resets"], ctx.log, rules)
                ctx.log(f"minimised to {mini['minimized']!r} in {mini['tries']} tries")
            board.recover()
        entry = {"build_id": fw.get("build_id"), "src_hash": fw.get("src_hash"), **rep,
                 "minimise": {k: v for k, v in mini.items()},
                 "recipe": recipe(f, mini["minimized"], fw)}
        history = f["reproductions"] + [entry]
        status = f["status"] if f["status"] in ("fixed", "wont_fix") else rep["status"]
        self.store.update_finding(f["id"], status=status, minimized=mini["minimized"],
                                  reproductions_json=json.dumps(history, default=str))
        return {"finding_id": f["id"], **{k: entry[k] for k in ("status", "hits", "attempts", "build_id")},
                "minimized": mini["minimized"], "recipe": entry["recipe"]}

    def finding(self, fid: int) -> dict:
        return self.store.finding(fid)

    def set_status(self, fid: int, status: str) -> dict:
        self.store.finding(fid)
        self.store.update_finding(fid, status=status)
        return self.store.finding(fid)

    # ------------------------------------------------------------ reading
    def campaign(self, cid: int) -> dict:
        camp = self.store.get(cid)
        camp["finding_list"] = self.store.findings(campaign_id=cid)
        return camp
