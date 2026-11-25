"""Audit hash chain, task records, replay and verification, reports, metrics and health."""
import json
import re
import sqlite3

import pytest

from app.audit import GENESIS, AuditLog
from app.records.replay import verify
from app.records.report import to_pdf

from conftest import auth
from fake_llm import DEFAULT_REPORT, FakeLLM
from test_agents import use
from test_pipeline import SPEC, approve, board_runs, builds, coder, resume, start, wait_pipe  # noqa: F401 (fixture)


def done_pipeline(client, services, board):
    """A pipeline the whole way: spec, code, build, scan, a human approves, tests on the board."""
    board_runs(1)
    use(services, FakeLLM(coder(), report=[SPEC, DEFAULT_REPORT]))
    p = wait_pipe(services, start(client)["id"])
    approve(client, p)
    resume(client, p)
    return wait_pipe(services, p["id"], ("FINISHED", "FAILED", "STOPPED"))


# ------------------------------------------------------------------ the audit chain
def test_the_audit_log_is_a_hash_chain(services):
    a = services.audit
    for i in range(5):
        a.write("tester", "agent", f"thing.{i}", "x")
    v = a.verify()
    assert v["ok"] and v["checked"] >= 5 and v["head"] == a.head()["hash"]
    db = sqlite3.connect(services.settings.db_path)
    for sql in ("UPDATE audit_log SET outcome = 'forged' WHERE id = 2", "DELETE FROM audit_log WHERE id = 2"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(sql)
    db.execute("DROP TRIGGER audit_no_update")                     # someone with file access goes around it
    db.execute("UPDATE audit_log SET target = 'changed' WHERE id = 3")
    db.commit()
    bad = a.verify()
    assert not bad["ok"] and bad["first_bad"] == 3 and "changed" in bad["reason"]


def test_removing_an_entry_breaks_the_link(services):
    a = services.audit
    for i in range(4):
        a.write("tester", "agent", f"x.{i}")
    db = sqlite3.connect(services.settings.db_path)
    db.execute("DROP TRIGGER audit_no_delete")
    db.execute("DELETE FROM audit_log WHERE id = 2")
    db.commit()
    v = a.verify()
    assert not v["ok"] and v["first_bad"] == 3 and "removed or reordered" in v["reason"]


def test_old_unchained_entries_are_chained(services):
    db = sqlite3.connect(services.settings.db_path)
    db.execute("INSERT INTO audit_log (ts, actor, role, action) VALUES ('2026-01-01T00:00:00+00:00', 'old', '-', 'legacy')")
    db.commit()
    a = AuditLog(services.settings.db_path)                         # chains what it finds on start
    assert a.verify()["ok"]
    first = db.execute("SELECT prev_hash FROM audit_log ORDER BY id LIMIT 1").fetchone()[0]
    assert first == GENESIS


def test_audit_endpoints(client):
    assert client.get("/audit/verify", headers=auth("viewer")).status_code == 403
    v = client.get("/audit/verify", headers=auth("operator")).json()
    assert v["ok"] and v["checked"] > 0
    assert client.get("/audit/head", headers=auth("viewer")).json()["hash"] == v["head"]


# ------------------------------------------------------------------ records, replay, verification
def test_a_pipeline_record_has_every_step(client, services, builds, board):
    p = done_pipeline(client, services, board)
    rec = client.get(f"/records/pipeline/{p['id']}", headers=auth("viewer")).json()
    kinds = sorted({j["kind"] for j in rec["jobs"]})
    assert kinds == ["build", "deploy", "pipeline", "test"]
    assert rec["counts"]["agents"] == 1 and rec["agents"][0]["llm_calls"] and rec["tokens"] > 0
    assert rec["builds"][0]["id"] == 1 and rec["builds"][0]["result"]["uf2_sha256"]
    assert rec["deployments"][0]["approved_by"].startswith("api:operator")
    assert rec["approvals"][0]["approved_by"] == rec["deployments"][0]["approved_by"] or rec["approvals"][0]["approved_by"]
    assert rec["test_runs"][0]["passed"] == 3 and rec["events"][0]["stage"] == "spec"
    assert any(a["action"] == "tool.write_source" for a in rec["audit"]) and len(rec["record_hash"]) == 64
    v = client.get(f"/records/pipeline/{p['id']}/verify", headers=auth("viewer")).json()
    assert v["ok"], v
    names = [c["check"] for c in v["checks"]]
    assert "audit chain" in names and "build #1 image" in names and "deployment #1 approval" in names
    replay = client.get(f"/records/pipeline/{p['id']}/replay", headers=auth("viewer")).json()
    ts = [e["ts"] for e in replay["events"]]
    assert ts == sorted(ts) and any("approved by" in e["what"] for e in replay["events"])


def test_verification_catches_a_swapped_image_and_an_edited_record(client, services, builds, board):
    p = done_pipeline(client, services, board)
    saved = client.post(f"/records/pipeline/{p['id']}/save", headers=auth("agent")).json()
    assert client.get(f"/records/saved/{saved['name']}/verify", headers=auth("viewer")).json()["ok"]
    path = services.settings.root / "data" / "records" / saved["name"]
    rec = json.loads(path.read_text())
    rec["deployments"][0]["approved_by"] = "api:operator (well, sort of)"
    path.write_text(json.dumps(rec))
    v = client.get(f"/records/saved/{saved['name']}/verify", headers=auth("viewer")).json()
    assert not v["ok"] and next(c for c in v["checks"] if c["check"] == "record")["status"] == "FAIL"
    (services.cfg.build.artifacts_dir / "1" / "firmware.uf2").write_bytes(b"not the image")
    v = client.get(f"/records/pipeline/{p['id']}/verify", headers=auth("viewer")).json()
    assert next(c for c in v["checks"] if c["check"] == "build #1 image")["status"] == "FAIL"
    assert client.get("/records/saved/../../etc/passwd/verify", headers=auth("viewer")).status_code == 404


def test_an_agent_approval_would_be_flagged(services):
    rec = {"record_hash": "", "audit": [], "builds": [], "agents": [], "approvals": [],
           "deployments": [{"id": 9, "build_id": None, "approved_by": "tool:agent", "image_sha256": "x"}]}
    v = verify(rec, services)
    assert any(c["check"] == "deployment #9 approval" and c["status"] == "FAIL" for c in v["checks"])


def test_records_of_unknown_tasks(client):
    assert client.get("/records/pipeline/99", headers=auth("viewer")).status_code == 404
    assert client.get("/records/spaceship/1", headers=auth("viewer")).status_code == 422
    assert client.get("/records/pipeline/1").status_code == 401


# ------------------------------------------------------------------ reports
def test_reports_in_three_formats(client, services, builds, board):
    p = done_pipeline(client, services, board)
    md = client.get(f"/records/pipeline/{p['id']}/report?format=md", headers=auth("viewer"))
    assert md.status_code == 200 and md.text.startswith(f"# Pipeline {p['id']}")
    for section in ("## Summary", "## Agents", "## Builds", "## Deployments and approvals", "## Verification",
                    "## Timeline"):
        assert section in md.text, section
    assert "api:operator" in md.text and "Result: **OK**" in md.text
    pdf = client.get(f"/records/pipeline/{p['id']}/report?format=pdf", headers=auth("viewer"))
    assert pdf.headers["content-type"] == "application/pdf" and "attachment" in pdf.headers["content-disposition"]
    body = pdf.content
    assert body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF")
    xref = int(re.search(rb"startxref\n(\d+)", body).group(1))
    assert body[xref:xref + 4] == b"xref"
    offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", body)]
    assert all(re.match(rb"%d 0 obj" % (i + 1), body[o:o + 12]) for i, o in enumerate(offsets))
    js = client.get(f"/records/pipeline/{p['id']}/report?format=json", headers=auth("viewer")).json()
    assert js["verification"]["ok"] and js["kind"] == "pipeline"
    assert client.get(f"/records/pipeline/{p['id']}/report?format=docx", headers=auth("viewer")).status_code == 422


def test_the_pdf_writer_pages_and_escapes():
    md = "# Title (with brackets)\n\n" + "\n".join(f"- line {i} \\ back\\slash é ünïcode ✓" for i in range(300))
    pdf = to_pdf(md, "t")
    assert int(re.search(rb"/Count (\d+)", pdf).group(1)) >= 3
    assert b"\\(with brackets\\)" in pdf and b"%%EOF" in pdf


# ------------------------------------------------------------------ observability
def test_metrics_and_health(client):
    client.post("/selftest", headers=auth("agent"), json={"seconds": 0})
    assert client.get("/metrics").status_code == 401
    r = client.get("/metrics", headers=auth("viewer"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    for name in ("aeisp_jobs_total", "aeisp_llm_tokens_total", "aeisp_audit_chain_ok 1", "aeisp_queue_length",
                 "aeisp_schema_version"):
        assert name in text, name
    assert re.search(r'aeisp_jobs_total\{kind="selftest",status="\w+"\} 1', text)
    h = client.get("/system/health/deep", headers=auth("viewer")).json()
    names = {c["check"]: c["status"] for c in h["checks"]}
    assert names["database"] == "ok" and names["audit chain"] == "ok" and h["status"] in ("ok", "warn", "fail")


def test_rebuild_from_the_record(client, services, builds, monkeypatch):
    monkeypatch.setattr("app.build.engine.build_project", builds)       # no toolchain in the tests
    from deploy_fakes import add_build

    add_build(services, 1, project="calc", files={"app.c": "int x;\n"})
    services.firmware.create_project("calc")
    r = client.post("/builds/1/rebuild", headers=auth("agent"))
    assert r.status_code == 202
    from test_api_services import wait_done

    job = wait_done(client, r.json()["id"], 60)
    assert job["status"] == "SUCCEEDED", job["error"]
    assert job["result"]["compared"]["sources_sha256"]["original"] == job["result"]["compared"]["sources_sha256"]["rebuild"]
    assert not (services.cfg.build.workspace_dir / "replay_b1").exists()          # the throw-away project is gone
