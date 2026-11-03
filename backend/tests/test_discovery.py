from types import SimpleNamespace

from app.hardware import discovery


def _fake(device, vid, pid, sn, desc):
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=sn, description=desc)


def test_finds_only_rp2040(monkeypatch):
    monkeypatch.setattr(
        discovery._lp,
        "comports",
        lambda: [
            _fake("COM3", None, None, None, "Bluetooth link"),
            _fake("COM8", 0x2E8A, 0x000A, "AAA", "Board CDC"),
            _fake("COM9", 0x1234, 0x0001, "BBB", "Other"),
        ],
    )
    found = discovery.find_rp2040_ports()
    assert [p.device for p in found] == ["COM8"]
    assert found[0].to_dict()["vid"] == "2E8A"


def test_filter_by_serial_number(monkeypatch):
    monkeypatch.setattr(
        discovery._lp,
        "comports",
        lambda: [_fake("COM8", 0x2E8A, 0x000A, "AAA", "x"), _fake("COM10", 0x2E8A, 0x000A, "CCC", "y")],
    )
    assert [p.device for p in discovery.find_rp2040_ports("CCC")] == ["COM10"]


def test_no_ports(monkeypatch):
    monkeypatch.setattr(discovery._lp, "comports", lambda: [])
    assert discovery.find_rp2040_ports() == []