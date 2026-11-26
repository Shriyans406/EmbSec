"""tools/demo.py, run against the test backend (fake board, fake builds, no model)."""
import importlib.util
from pathlib import Path

from conftest import TOKENS, FakeLink
from deploy_fakes import add_build
from test_api_services import make_stock, new_project
from test_fuzz import campaign, fw  # noqa: F401  (fw is a fixture)

ROOT = Path(__file__).resolve().parents[2]


def load_demo():
    spec = importlib.util.spec_from_file_location("aeisp_demo", ROOT / "tools" / "demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_demo(client, **kw):
    lines = []
    demo = load_demo().Demo(client, dict(TOKENS), out=lines.append, poll_s=0.05, timeout_s=60)
    code = demo.run(**kw)
    return code, demo.results, "\n".join(lines)


def test_the_demo_runs_clean_with_a_board(client, services, monkeypatch):
    make_stock(services)
    new_project(client)

    def fake_build(project, timeout_s=None, cfg=None, on_stage=None):
        from app.build.engine import BuildResult

        add_build(services, 1, project=project)
        return BuildResult(id=1, project=project, status="SUCCESS", message="ok", flash_bytes=40000, ram_bytes=9000)

    monkeypatch.setattr("app.services.builds.build_project", fake_build)
    code, results, text = run_demo(client, project="demo")
    status = {name.split(". ", 1)[1]: s for name, s, _ in results}
    assert code == 0, text
    assert status["the board"] == "ok" and status["smoke tests on the board"] == "ok", text
    assert status["roles and approvals"] == "ok" and status["audit chain"] == "ok", text
    assert "passed the gate" in text and "never flashes" in text
    assert not any(t in text for t in TOKENS.values())                         # tokens are never printed
    jobs = client.get("/jobs?kind=deploy", headers={"Authorization": f"Bearer {TOKENS['viewer']}"}).json()
    assert all(j["status"] == "CANCELLED" for j in jobs)                     # nothing is left waiting


def test_board_steps_are_skipped_without_a_board(client):
    FakeLink.fail_connect = True
    code, results, text = run_demo(client)
    status = {name.split(". ", 1)[1]: s for name, s, _ in results}
    assert status["the board"] == "skip" and status["smoke tests on the board"] == "skip", text
    assert code == 0, text


def test_tokens_come_from_dotenv(tmp_path, monkeypatch):
    for r in ("VIEWER", "AGENT", "OPERATOR"):
        monkeypatch.delenv(f"AEISP_{r}_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nAEISP_VIEWER_TOKEN="v1"\nAEISP_AGENT_TOKEN=a1\nGEMINI_API_KEY=x\n', encoding="utf-8")
    assert load_demo().read_tokens(env) == {"viewer": "v1", "agent": "a1"}


def test_findings_and_records_with_a_fuzz_campaign(client, services, fw):
    fw.bugs.add("no_range_check")
    campaign(client, families=["overflow", "boundary"], max_inputs=150)
    code, results, text = run_demo(client)
    status = {name.split(". ", 1)[1]: (s, d) for name, s, d in results}
    assert status["fuzzing and findings"][0] == "ok" and "CWE-20" in text, text
    assert status["task records and reports"] == ("ok", status["task records and reports"][1]), text
    assert "fuzz_campaign #1: record verified" in text, text
    assert code == 0, text


def test_the_ai_step_with_a_scripted_model(client, services):
    from fake_llm import FakeLLM, call, say
    from test_agents import use

    use(services, FakeLLM([call("device_command", command="PING"), say("The board answers PING.")]))
    code, results, text = run_demo(client, ai=True)
    assert results[-1][1] == "ok" and "The board answers PING." in text, text
    assert code == 0, text
