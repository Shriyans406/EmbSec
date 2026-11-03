from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.hardware import api, discovery
from app.main import app

client = TestClient(app)


def test_health_phase():
    assert client.get("/health").json() == {"status": "ok", "phase": 1}


def test_ports_endpoint(monkeypatch):
    monkeypatch.setattr(
        discovery._lp,
        "comports",
        lambda: [SimpleNamespace(device="COM8", vid=0x2E8A, pid=0xA, serial_number="X", description="Board CDC")],
    )
    r = client.get("/device/ports")
    assert r.status_code == 200
    assert r.json()[0]["is_rp2040"] is True


def test_disallowed_command_is_rejected():
    r = client.post("/device/command", json={"command": "BOOTSEL"})
    assert r.status_code == 422


def test_empty_command_is_rejected():
    assert client.post("/device/command", json={"command": ""}).status_code == 422


def test_status_when_no_board(monkeypatch):
    monkeypatch.setattr(discovery._lp, "comports", lambda: [])
    api.link.close()
    r = client.get("/device/status").json()
    assert r["connected"] is False and r["error"]