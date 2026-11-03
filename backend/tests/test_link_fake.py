import json

import pytest
import serial

from app.hardware.link import LinkError, LinkTimeout, NotConnected, SerialLink

UID = "E661A4D1234567AB"


class FakeSerial:
    """Behaves like the Phase 1 firmware, in memory."""

    instances = []
    fail_open = False
    drop_replies = 0  # number of upcoming commands to swallow (simulates timeout)

    def __init__(self, port, baud, timeout=1.0, write_timeout=1.0):
        if FakeSerial.fail_open:
            raise serial.SerialException("could not open port")
        self.port, self.timeout = port, timeout
        self.is_open = True
        self.dtr = False
        self._out = b""
        self._unplugged = False
        FakeSerial.instances.append(self)

    def reset_input_buffer(self):
        self._out = b""

    def write(self, data):
        if self._unplugged:
            raise serial.SerialException("device disconnected")
        line = data.decode().rstrip("\n")
        if FakeSerial.drop_replies > 0:
            FakeSerial.drop_replies -= 1
            return
        if line == "HELLO":
            r = f"OK HELLO AEISP-ECHO 0.1.0 {UID}"
        elif line == "PING":
            r = "OK PONG"
        elif line.startswith("ECHO "):
            r = "OK " + line[5:]
        else:
            r = "ERR UNKNOWN"
        self._out += (r + "\n").encode()

    def flush(self):
        pass

    def readline(self):
        if self._unplugged:
            raise serial.SerialException("device disconnected")
        out, self._out = self._out, b""
        return out

    def close(self):
        self.is_open = False

    def unplug(self):
        self._unplugged = True


@pytest.fixture(autouse=True)
def _reset():
    FakeSerial.instances = []
    FakeSerial.fail_open = False
    FakeSerial.drop_replies = 0


def make(tmp_path, **kw):
    return SerialLink(port="COM8", log_path=tmp_path / "serial.jsonl", serial_factory=FakeSerial, timeout=0.1, **kw)


def test_connect_and_handshake(tmp_path):
    link = make(tmp_path)
    info = link.connect()
    assert (info.firmware, info.version, info.uid, info.port) == ("AEISP-ECHO", "0.1.0", UID, "COM8")
    assert link.is_connected
    assert FakeSerial.instances[0].dtr is True


def test_command_ok_and_err(tmp_path):
    link = make(tmp_path)
    link.connect()
    assert link.command("PING") == "PONG"
    assert link.command("ECHO abc123") == "abc123"
    with pytest.raises(LinkError):
        link.command("NOPE")


def test_timeout_raises(tmp_path):
    link = make(tmp_path)
    link.connect()
    FakeSerial.drop_replies = 1
    with pytest.raises(LinkTimeout):
        link.request("PING", retries=0)


def test_retry_after_timeout_succeeds(tmp_path):
    link = make(tmp_path)
    link.connect()
    FakeSerial.drop_replies = 1
    assert link.request("PING", retries=1) == "OK PONG"


def test_open_failure_is_not_connected(tmp_path):
    FakeSerial.fail_open = True
    with pytest.raises(NotConnected):
        make(tmp_path).connect()


def test_unplug_then_reconnect(tmp_path):
    link = make(tmp_path)
    link.connect()
    FakeSerial.instances[0].unplug()
    with pytest.raises(NotConnected):
        link.request("PING", retries=0)
    assert not link.is_connected
    # "replug": next request reconnects by itself
    assert link.request("PING", retries=1, reconnect_wait=2) == "OK PONG"
    assert link.is_connected
    assert len(FakeSerial.instances) == 2


def test_rejects_line_breaks(tmp_path):
    link = make(tmp_path)
    link.connect()
    with pytest.raises(ValueError):
        link.request("PING\nPING")


def test_log_file_records_traffic(tmp_path):
    link = make(tmp_path)
    link.connect()
    link.request("PING")
    recs = [json.loads(x) for x in (tmp_path / "serial.jsonl").read_text().splitlines()]
    pairs = [(r["dir"], r["text"]) for r in recs]
    assert ("TX", "PING") in pairs and ("RX", "OK PONG") in pairs
    assert all("ts" in r for r in recs)