"""Replaying a task from its record.

verify(record)   checks the record against what exists now, item by item:
    record      the bundle's own hash (a saved record that was edited no longer matches)
    audit       the audit chain is intact, and every audit entry in the record is still there with the same hash
    builds      the image on disk still has the recorded SHA-256; the saved sources still hash to the recorded value
    deployments each deployment flashed exactly the image of its build, and a HUMAN approved it (never an agent)
    agents      every tool call an agent made appears in the audit log
rebuild(build)   builds the saved sources of a build again (in a throw-away project) and compares the result:
                 same sources hash, same harness, same flash / RAM size, same warnings and errors.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import List

from ..build.engine import hash_sources
from ..db import connect
from ..errors import ApiError
from ..jobs import JobContext, JobFailed
from .record import canonical_hash, timeline

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def _check(out: list, name: str, ok, detail: str, warn: bool = False) -> None:
    out.append({"check": name, "status": PASS if ok else (WARN if warn else FAIL), "detail": detail})


def verify(rec: dict, services) -> dict:
    out: List[dict] = []
    body = {k: v for k, v in rec.items() if k not in ("taken_at", "record_hash", "verification")}
    _check(out, "record", canonical_hash(body) == rec.get("record_hash"),
           "the bundle matches its hash" if canonical_hash(body) == rec.get("record_hash")
           else "the bundle does not match its hash: it was changed after it was taken")

    chain = services.audit.verify()
    _check(out, "audit chain", chain["ok"], f"{chain['checked']} entries chained" if chain["ok"]
           else f"broken at entry #{chain['first_bad']}: {chain['reason']}")
    ids = [a["id"] for a in rec.get("audit", [])]
    if ids:
        with connect(services.settings.db_path) as c:
            now = {r["id"]: r["hash"] for r in c.execute(
                f"SELECT id, hash FROM audit_log WHERE id IN ({','.join('?' * len(ids))})", ids)}
        changed = [a["id"] for a in rec["audit"] if a.get("hash") and now.get(a["id"]) != a["hash"]]
        _check(out, "audit entries", not changed, f"{len(ids)} entries of the task are unchanged" if not changed
               else f"entries {changed[:10]} differ from the record")

    for b in rec.get("builds", []):
        res = b.get("result") or {}
        adir = Path(b.get("artifact_dir") or "")
        uf2 = adir / "firmware.uf2"
        if not uf2.is_file():
            _check(out, f"build #{b['id']} image", False, "the image is gone from disk", warn=res.get("status") != "SUCCESS")
        else:
            sha = hashlib.sha256(uf2.read_bytes()).hexdigest()
            _check(out, f"build #{b['id']} image", sha == res.get("uf2_sha256"),
                   f"sha256 {sha[:12]} {'matches' if sha == res.get('uf2_sha256') else 'DIFFERS FROM'} the record")
        snap = adir / "app_src"
        if snap.is_dir():
            files = [p for p in snap.iterdir() if p.is_file()]
            src = hash_sources(files) if files else ""
            _check(out, f"build #{b['id']} sources", src == (res.get("sources_sha256") or b.get("sources_sha256")),
                   f"saved sources hash {src[:8]} {'matches' if src == res.get('sources_sha256') else 'differs'}")

    images = {b["id"]: (b.get("result") or {}).get("uf2_sha256") for b in rec.get("builds", [])}
    for d in rec.get("deployments", []):
        if d.get("build_id") in images:
            _check(out, f"deployment #{d['id']} image", d.get("image_sha256") == images[d["build_id"]],
                   "flashed exactly the build's image" if d.get("image_sha256") == images[d["build_id"]]
                   else "flashed a different image than the build's")
        who = d.get("approved_by") or ""
        human = bool(who) and not who.startswith(("tool:", "run:", "pipeline", "remediation", "optimisation")) \
            and "agent" not in who.split(" ")[0]
        _check(out, f"deployment #{d['id']} approval", human, f"approved by {who or 'nobody'}")
    for a in rec.get("approvals", []):
        if a["status"] in ("SUCCEEDED", "FAILED") and a.get("approved_by"):
            _check(out, f"proposal job #{a['job_id']}", not a["approved_by"].startswith(("tool:", "run:")),
                   f"approved by {a['approved_by']}")

    for run in rec.get("agents", []):
        calls = [s["name"] for s in run.get("steps", []) if s["kind"] == "tool" and s["name"] != "delegate"]
        audited = [a["action"][5:] for a in rec.get("audit", []) if a["action"].startswith("tool.")]
        missing = [n for n in set(calls) if calls.count(n) > audited.count(n)]
        _check(out, f"agent run #{run['id']} tool calls", not missing,
               f"{len(calls)} tool calls, all in the audit log" if not missing
               else f"tool calls without an audit entry: {missing}", warn=True)

    fails = sum(1 for c in out if c["status"] == FAIL)
    return {"ok": fails == 0, "checks": out, "failed": fails, "warnings": sum(1 for c in out if c["status"] == WARN),
            "timeline": len(timeline(rec))}


# ---------------------------------------------------------------- rebuild from the saved sources
def start_rebuild(services, build_id: int, who: str) -> dict:
    b = services.builds.get(build_id)
    snap = Path(b.get("artifact_dir") or "") / "app_src"
    if not snap.is_dir():
        raise ApiError(409, "no_sources", f"build #{build_id} has no saved sources")
    return services.jobs.submit("rebuild", {"build_id": build_id}, who, summary=f"rebuild #{build_id} from its record")


def run_rebuild(services, ctx: JobContext) -> dict:
    from ..build.engine import build_project

    orig = services.builds.get(ctx.params["build_id"])
    name = f"replay_b{orig['id']}"
    pdir = services.cfg.build.workspace_dir / name
    if pdir.exists():
        shutil.rmtree(pdir)
    services.firmware.create_project(name)
    app = pdir / "app"
    for p in app.iterdir():
        p.unlink()
    for p in (Path(orig["artifact_dir"]) / "app_src").iterdir():
        shutil.copyfile(p, app / p.name)
    try:
        res = build_project(name, cfg=services.cfg.build, on_stage=ctx.log).to_dict()
    finally:
        shutil.rmtree(pdir, ignore_errors=True)
    same = {
        "status": (orig["status"], res["status"]),
        "sources_sha256": (orig["sources_sha256"], res["sources_sha256"]),
        "harness_version": (orig.get("harness_version"), res.get("harness_version")),
        "flash_bytes": (orig.get("flash_bytes"), res.get("flash_bytes")),
        "ram_bytes": (orig.get("ram_bytes"), res.get("ram_bytes")),
        "errors": (orig.get("errors"), res.get("errors")),
        "warnings": (orig.get("warnings"), res.get("warnings")),
    }
    diffs = {k: v for k, v in same.items() if v[0] != v[1]}
    out = {"original": orig["id"], "rebuild": res["id"], "identical": not diffs,
           "compared": {k: {"original": v[0], "rebuild": v[1]} for k, v in same.items()},
           "note": "the image itself differs by design: each build embeds its own build number"}
    ctx.log(f"rebuild of #{orig['id']} as #{res['id']}: {'identical' if not diffs else 'DIFFERENT: ' + json.dumps(diffs)}")
    if diffs and "harness_version" not in diffs:
        raise JobFailed(f"the rebuild of #{orig['id']} differs: {sorted(diffs)}", out)
    return out
