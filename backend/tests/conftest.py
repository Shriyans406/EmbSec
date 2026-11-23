"""Shared test helpers for the backend API tests."""
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.hardware.link import LinkError, NotConnected
from app.main import create_app
from app.services.container import build_services
from app.services.device import DeviceManager
from app.settings import Settings

from deploy_fakes import FakeBoard

TOKENS = {"operator": "op-token", "agent": "ag-token", "viewer": "vi-token"}


def auth(role):
    return {"Authorization": f"Bearer {TOKENS[role]}"}


@dataclass
class _Info:
    port: str = "COM8"
    firmware: str = "AEISP-DEV"
    version: str = "0.3.0"
    uid: str = "DE61C48787331A35"

    def to_dict(self):
        return self.__dict__.copy()


DEFAULT_REPLIES = {
    "PING": "OK PONG",
    "HELLO": "OK HELLO AEISP-DEV 0.3.0 DE61C48787331A35",
    "VERSION": "OK VERSION fw=0.3.0 proto=1 sdk=1.5.1 build=1 src=277d6f81",
    "INFO": "OK INFO name=AEISP-DEV fw=0.3.0 proto=1 uid=DE61C48787331A35 board=shrike-lite mcu=rp2040 clk_mhz=125",
    "STATUS": "OK STATUS uptime_ms=5 led=OFF blink_ms=0 timer=IDLE cmds=3 errors=0",
    "ECHO hello": "OK hello",
    "NOPE": "ERR 1 UNKNOWN_CMD",
    "PING now": "ERR 2 BAD_ARGS PING takes no arguments",
    "BLINK 20": "ERR 3 BAD_RANGE interval must be 0 or 50..10000 ms",
    "LED ON": "OK LED state=ON",
    "LED GET": "OK LED state=ON",
    "LED OFF": "OK LED state=OFF",
    "APP": "ERR 2 BAD_ARGS usage: APP <command>",
}


class FakeLink:
    """Stands in for the serial connection; answers like the firmware."""

    fail_connect = False
    fail_after = None          # raise LinkError once this many commands have been sent
    replies = dict(DEFAULT_REPLIES)
    sent = []

    def __init__(self):
        self.is_connected = False
        self.info = None

    def connect(self, handshake_tries=3):
        if FakeLink.fail_connect:
            raise NotConnected("no RP2040 serial port found (USB vendor ID 2E8A)")
        self.is_connected, self.info = True, _Info()
        return self.info

    def request(self, text, retries=0, reconnect_wait=5.0):
        if not self.is_connected:
            self.connect()
        if FakeLink.fail_after is not None and len(FakeLink.sent) >= FakeLink.fail_after:
            self.is_connected = False
            raise LinkError("link lost: device disconnected")
        FakeLink.sent.append(text)
        return FakeLink.replies.get(text, "OK")

    def close(self):
        self.is_connected = False


@pytest.fixture(autouse=True)
def _reset_fake_link():
    FakeLink.fail_connect = False
    FakeLink.fail_after = None
    FakeLink.replies = dict(DEFAULT_REPLIES)
    FakeLink.sent = []


def make_settings(tmp_path, **kw):
    return Settings(_env_file=None, root=tmp_path, operator_token=TOKENS["operator"],
                    agent_token=TOKENS["agent"], viewer_token=TOKENS["viewer"], **kw)


@pytest.fixture()
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture()
def board():
    return FakeBoard()


@pytest.fixture()
def app(settings, board):
    services = build_services(settings)
    services.device = DeviceManager(link_factory=FakeLink)
    services.deploys.device = services.device
    services.tests.device = services.device
    services.analysis.device = services.device
    services.deploys.board_factory = lambda: board          # no real board is ever touched in tests
    return create_app(settings, services)


@pytest.fixture()
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def services(app):
    return app.state.services
