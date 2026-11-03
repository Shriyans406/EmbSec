"""Hardware-in-the-loop tests. The board must be plugged in and running the Phase 1 firmware.
Run with:  python -m pytest -m hardware -v   (stop the backend server first: it also uses the COM port)
"""
import random

import pytest

from app.hardware.discovery import find_rp2040_ports
from app.hardware.link import SerialLink

pytestmark = pytest.mark.hardware


@pytest.fixture()
def link():
    l = SerialLink()
    l.connect()
    yield l
    l.close()


def test_board_is_visible_on_usb():
    assert find_rp2040_ports(), "no RP2040 serial port (VID 2E8A) found"


def test_handshake(link):
    assert link.info.firmware == "AEISP-ECHO"
    assert link.info.version == "0.1.0"
    assert len(link.info.uid) == 16


def test_ping(link):
    assert link.request("PING") == "OK PONG"


def test_uptime_increases(link):
    a = int(link.command("UPTIME").split()[1])
    import time

    time.sleep(0.3)
    b = int(link.command("UPTIME").split()[1])
    assert b > a


def test_echo_200_random_payloads(link):
    rng = random.Random(7)
    for _ in range(200):
        p = "".join(rng.choice("abcdef0123456789") for _ in range(rng.randint(1, 60)))
        assert link.request(f"ECHO {p}", retries=0) == f"OK {p}"


def test_unknown_command_rejected(link):
    assert link.request("HELLO_WORLD", retries=0) == "ERR UNKNOWN"
    assert link.request("ping", retries=0) == "ERR UNKNOWN"  # case sensitive


def test_echo_without_text_is_usage_error(link):
    assert link.request("ECHO", retries=0) == "ERR USAGE"


def test_too_long_line_rejected_and_device_recovers(link):
    assert link.request("A" * 300, retries=0) == "ERR TOOLONG"
    assert link.request("PING", retries=0) == "OK PONG"


def test_exactly_max_length_line_is_accepted(link):
    text = "x" * (128 - len("ECHO "))
    assert link.request(f"ECHO {text}", retries=0) == f"OK {text}"