from app.hardware.protocol import add_crc, build_request, crc16, parse_reply

# These expected values were produced by the firmware's own C code (host_test.c), so a match here
# proves the Python and C checksum implementations agree.
FIRMWARE_VECTORS = {
    "PING": "6427",
    "#7 PING": "4824",
    "LED ON": "CB8F",
    "LED OFF": "468C",
    "STATUS": "2DAA",
    "NONSENSE": "8851",
    "BLINK 500": "AD72",
    "BLINK 20": "981C",
    "ECHO hello": "6AAB",
}


def test_crc_reference_vector():
    assert crc16("123456789") == 0x29B1


def test_crc_matches_firmware_vectors():
    for text, want in FIRMWARE_VECTORS.items():
        assert f"{crc16(text):04X}" == want, text


def test_add_crc_and_build_request():
    assert add_crc("PING") == "PING*6427"
    assert build_request("PING", req_id=7, with_crc=True) == "#7 PING*4824"
    assert build_request("LED ON") == "LED ON"


def test_parse_ok_reply():
    r = parse_reply("OK LED state=ON")
    assert r.ok and r.fields == {"state": "ON"} and r.crc_ok is None and r.req_id is None


def test_parse_reply_with_id_and_good_crc():
    r = parse_reply("#7 OK PONG*F37E")
    assert r.ok and r.req_id == 7 and r.crc_present and r.crc_ok is True and r.body == "PONG"


def test_parse_reply_with_bad_crc():
    assert parse_reply("OK PONG*0000").crc_ok is False


def test_parse_error_reply():
    r = parse_reply("ERR 3 BAD_RANGE interval must be 0 or 50..10000 ms")
    assert not r.ok and r.code == 3 and r.name == "BAD_RANGE" and "50..10000" in r.detail


def test_parse_error_with_crc():
    r = parse_reply("ERR 1 UNKNOWN_CMD*E000")
    assert r.code == 1 and r.crc_ok is True