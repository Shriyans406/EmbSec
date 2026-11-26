"""End-to-end demo of AEISP against a running backend.

    python tools\\demo.py                       everything that needs no model and changes nothing on the board
    python tools\\demo.py --project calc2       also build that project and show the pre-flash gate
    python tools\\demo.py --ai                  also run one agent (uses 1-3 Gemini requests of the daily 20)

Start the backend first (python -m uvicorn app.main:app --port 8000, from the backend folder).
The tokens are read from .env. They are never printed.

The demo never flashes. A proposed deployment is shown with its gate checks and then cancelled, because
only a person with the operator token may approve one.
Steps that need the board are skipped, with a note, when no board is connected.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
FINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED")


def read_tokens(env_path: Path = ROOT / ".env") -> Dict[str, str]:
    """Tokens from the environment, or from .env."""
    vals: Dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    out = {}
    for role in ("viewer", "agent", "operator"):
        key = f"AEISP_{role.upper()}_TOKEN"
        tok = os.environ.get(key) or vals.get(key)
        if tok:
            out[role] = tok
    return out


class Demo:
    def __init__(self, client, tokens: Dict[str, str], out: Callable[[str], None] = print,
                 poll_s: float = 0.5, timeout_s: float = 300) -> None:
        self.c = client
        self.tokens = tokens
        self.out = out
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self.results: List[tuple] = []       # (step, "ok" | "skip" | "fail", detail)
        self.board = False

    # ------------------------------------------------------------ helpers
    def h(self, role: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def get(self, path: str, role: str = "viewer", **kw):
        return self.c.get(path, headers=self.h(role), **kw)

    def post(self, path: str, role: str = "agent", json: Optional[dict] = None):
        return self.c.post(path, headers=self.h(role), json=json or {})

    def wait(self, job_id: int) -> dict:
        end = time.time() + self.timeout_s
        while time.time() < end:
            j = self.get(f"/jobs/{job_id}").json()
            if j["status"] in FINAL:
                return j
            time.sleep(self.poll_s)
        raise TimeoutError(f"job #{job_id} did not finish within {self.timeout_s:.0f} s")

    def step(self, name: str, fn: Callable[[], Optional[str]]) -> None:
        self.out(f"\n== {name}")
        try:
            detail = fn()
            status = "ok"
            if isinstance(detail, tuple):
                status, detail = detail
        except Exception as e:  # noqa: BLE001  one broken step must not hide the others
            status, detail = "fail", f"{type(e).__name__}: {e}"
        self.results.append((name, status, detail or ""))
        self.out(f"   [{status}] {detail or ''}")

    def say(self, text: str) -> None:
        self.out(f"   {text}")

    # ------------------------------------------------------------ the steps
    def health(self):
        r = self.c.get("/health")
        r.raise_for_status()
        info = self.get("/system/info").json()
        self.say(f"backend {info.get('version')} (database schema {info.get('schema_version')}), "
                 f"you are '{info['you']['role']}'")
        deep = self.get("/system/health/deep").json()
        for c in deep["checks"]:
            self.say(f"{c['status']:<5} {c['check']}: {c.get('detail', '')}")
        bad = [c["check"] for c in deep["checks"] if c["status"] == "fail"]
        return ("fail", f"failing checks: {', '.join(bad)}") if bad else f"overall {deep['status']}"

    def roles(self):
        viewer_write = self.post("/selftest", role="viewer").status_code
        agent_approve = self.c.post("/jobs/1/approve", headers=self.h("agent"), json={"confirm": "x"}).status_code
        no_token = self.c.get("/system/info").status_code
        self.say(f"viewer starting a job -> {viewer_write}, agent approving -> {agent_approve}, no token -> {no_token}")
        if (viewer_write, agent_approve, no_token) != (403, 403, 401):
            return "fail", "a role could do more than it should"
        return "viewer is read-only, agents cannot approve, and a request without a token is refused"

    def board_status(self):
        st = self.get("/device/status").json()
        self.board = bool(st.get("connected"))
        if not self.board:
            return "skip", f"no board: {st.get('error') or 'not connected'} (board steps will be skipped)"
        r = self.post("/device/command", json={"command": "PING"}).json()
        self.say(f"PING -> {r.get('reply') or r}")
        info = st.get("info") or {}
        return f"board on {info.get('port', '?')}, firmware {info.get('firmware', '?')} {info.get('version', '')}".strip()

    def smoke(self):
        if not self.board:
            return "skip", "needs the board"
        job = self.wait(self.post("/tests/run", json={"suite": "smoke"}).json()["id"])
        res = job.get("result") or {}
        self.say(f"{res.get('passed', '?')} passed, {res.get('failed', '?')} failed")
        return ("ok" if job["status"] == "SUCCEEDED" else "fail"), f"smoke suite: {job['status']}"

    def build(self, project: str):
        r = self.post("/builds", json={"project": project})
        if r.status_code >= 400:
            return "fail", f"could not start the build: {r.json().get('error', {}).get('message', r.text)}"
        job = self.wait(r.json()["id"])
        res = job.get("result") or {}
        self.build_id = res.get("id")
        self.say(f"build #{self.build_id}: {res.get('status')} - {res.get('message')}")
        self.say(f"flash {res.get('flash_bytes', 0)} B ({res.get('flash_pct', 0)}%), ram {res.get('ram_bytes', 0)} B, "
                 f"{res.get('errors', 0)} errors, {res.get('warnings', 0)} warnings")
        return ("ok" if job["status"] == "SUCCEEDED" else "fail"), f"build {job['status']}"

    def gate(self):
        bid = getattr(self, "build_id", None)
        if not bid:
            return "skip", "no build to check"
        r = self.post("/deployments", json={"build_id": bid})
        body = r.json()
        if r.status_code >= 400:
            err = body.get("error", {})
            for c in (err.get("details") or {}).get("checks", []):
                self.say(f"{c['status']:<5} {c['name']}: {c['detail']}")
            return f"the gate refused build #{bid}: {err.get('code')} - {err.get('message')}"
        job = self.get(f"/jobs/{body['id']}").json()
        for c in job["params"].get("checks", []):
            self.say(f"{c['status']:<5} {c['name']}: {c['detail']}")
        self.say(f"job #{job['id']} is {job['status']}: only the operator can approve it (typed confirmation)")
        self.post(f"/jobs/{job['id']}/cancel")
        return f"build #{bid} passed the gate; the proposal was cancelled again (the demo never flashes)"

    def fuzz_and_findings(self):
        camps = self.get("/fuzz/campaigns").json()
        finds = self.get("/fuzz/findings").json()
        self.say(f"{len(camps)} fuzz campaigns, {len(finds)} findings in the lab history")
        for f in finds[:5]:
            self.say(f"#{f['id']} {f.get('severity', '')} {f.get('kind', '')} {f.get('cwe') or ''} "
                     f"[{f.get('status', '')}]: {(f.get('detail') or '')[:60]}  input {f.get('input_text', '')[:30]!r}")
        return f"{len(finds)} findings"

    def knowledge(self):
        stats = self.get("/knowledge/stats").json()
        hits = self.get("/knowledge/search", params={"q": "watchdog"}).json()
        hits = hits.get("results", hits) if isinstance(hits, dict) else hits
        self.say(f"knowledge base: {stats.get('total', stats)} entries; 'watchdog' finds {len(hits)}")
        for h in hits[:3]:
            self.say(f"- [{h.get('kind')}] {h.get('title', '')[:70]}")
        return "searchable lab memory"

    def audit(self):
        if "operator" not in self.tokens:
            return "skip", "needs the operator token"
        v = self.get("/audit/verify", role="operator").json()
        return ("ok" if v.get("ok") else "fail"), f"audit chain of {v.get('checked', '?')} entries: " \
                                                  f"{'intact' if v.get('ok') else 'BROKEN at #' + str(v.get('first_bad')) + ': ' + str(v.get('reason'))}"

    def records(self):
        for kind, path in (("fuzz_campaign", "/fuzz/campaigns"), ("pipeline", "/pipelines"),
                           ("remediation", "/remediations"), ("agent_run", "/agent/runs")):
            rows = self.get(path).json()
            if rows:
                tid = rows[0]["id"]
                v = self.get(f"/records/{kind}/{tid}/verify").json()
                rep = self.get(f"/records/{kind}/{tid}/report", params={"format": "md"})
                self.say(f"{kind} #{tid}: record {'verified' if v.get('ok') else 'NOT verified'}, "
                         f"report {len(rep.text)} characters")
                return ("ok" if v.get("ok") and rep.status_code == 200 else "fail"), f"{kind} #{tid} record + report"
        return "skip", "no finished task to report on yet"

    def metrics(self):
        text = self.get("/metrics").text
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        for ln in lines[:6]:
            self.say(ln)
        return f"{len(lines)} Prometheus samples"

    def agent(self):
        st = self.get("/agent/status").json()
        if not st.get("configured", st.get("ai", {}).get("configured")):
            return "skip", "GEMINI_API_KEY is not set"
        r = self.post("/agent/runs", json={"goal": "Check that the board answers PING and report its firmware "
                                                    "version. Do not change anything.", "max_steps": 4})
        rid = r.json()["id"]
        end = time.time() + self.timeout_s
        while time.time() < end:
            run = self.get(f"/agent/runs/{rid}").json()
            if run["status"] in ("FINISHED", "FAILED", "STOPPED"):
                break
            time.sleep(self.poll_s)
        self.say((run.get("answer") or run.get("error") or f"still {run['status']}")[:300])
        return ("ok" if run["status"] == "FINISHED" else "fail"), f"agent run #{rid}: {run['status']}"

    # ------------------------------------------------------------ all of it
    def run(self, project: Optional[str] = None, ai: bool = False) -> int:
        missing = [r for r in ("viewer", "agent") if r not in self.tokens]
        if missing:
            self.out(f"missing tokens: {', '.join(missing)} (run: python -m app.cli make-tokens)")
            return 2
        self.step("1. backend health", self.health)
        self.step("2. roles and approvals", self.roles)
        self.step("3. the board", self.board_status)
        self.step("4. smoke tests on the board", self.smoke)
        if project:
            self.step(f"5. build '{project}'", lambda: self.build(project))
            self.step("6. pre-flash gate (security scan, size, hashes)", self.gate)
        self.step("7. fuzzing and findings", self.fuzz_and_findings)
        self.step("8. knowledge base", self.knowledge)
        self.step("9. audit chain", self.audit)
        self.step("10. task records and reports", self.records)
        self.step("11. metrics", self.metrics)
        if ai:
            self.step("12. one AI agent run", self.agent)
        ok = sum(1 for r in self.results if r[1] == "ok")
        skip = sum(1 for r in self.results if r[1] == "skip")
        fail = [r[0] for r in self.results if r[1] == "fail"]
        self.out(f"\n{ok} ok, {skip} skipped, {len(fail)} failed" + (f": {', '.join(fail)}" if fail else ""))
        return 1 if fail else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--project", help="also build this project and show the pre-flash gate")
    ap.add_argument("--ai", action="store_true", help="also run one AI agent (uses Gemini requests)")
    args = ap.parse_args(argv)
    import httpx

    with httpx.Client(base_url=args.url, timeout=30) as client:
        try:
            client.get("/health")
        except httpx.HTTPError:
            print(f"the backend is not running at {args.url}. Start it from the backend folder:\n"
                  f"   python -m uvicorn app.main:app --port 8000")
            return 2
        return Demo(client, read_tokens()).run(args.project, args.ai)


if __name__ == "__main__":
    sys.exit(main())
