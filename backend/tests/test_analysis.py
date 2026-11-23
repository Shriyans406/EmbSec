"""The security scanner, the size report and the latency tool."""
import pytest

from app.analysis.security_scan import scan_sources, strip_code
from app.build.engine import FAILED as BUILD_FAILED

from conftest import FakeLink, auth
from deploy_fakes import add_build

GOOD = """#include <stdio.h>
#include <string.h>
#include "app_api.h"
/* printf("comments are ignored"); gpio_put(13, 1); */
static unsigned ticks;
void app_init(void) { }
void app_tick(void) { ticks++; }
int app_command(const char *args, char *out, size_t cap) {
    if (strcmp(args, "TICKS") == 0) { snprintf(out, cap, "ticks=%u", ticks); return APP_OK; }
    snprintf(out, cap, "say printf( or strcpy( in a string: fine");
    return APP_ERR_ARGS;
}
"""


def rules(text, name="app.c", keep_info=False):
    return [(f["rule"], f["line"]) for f in scan_sources({name: text})["findings"]
            if keep_info or f["rule"] != "SEC013"]


def test_clean_code_passes():
    r = scan_sources({"app.c": GOOD})
    assert r["verdict"] == "PASS" and r["findings"] == [] and r["counts"]["high"] == 0


@pytest.mark.parametrize("line,rule", [
    ("void f(void) { gpio_init(12); }", "SEC001"),
    ("void f(void) { gpio_put(13, 1); }", "SEC001"),
    ("void f(void) { gpio_set_mask(1u << 12); }", "SEC001"),
    ('void f(void) { printf("hi"); }', "SEC002"),
    ("void f(void) { stdio_init_all(); }", "SEC002"),
    ("void f(char *o) { strcpy(o, o); }", "SEC003"),
    ('void f(char *o) { sprintf(o, "%d", 1); }', "SEC003"),
    ("void f(void) { reset_usb_boot(0, 0); }", "SEC004"),
    ('#include "hardware/flash.h"', "SEC004"),
    ("#include <pico/multicore.h>", "SEC004"),
    ("void f(void) { watchdog_reboot(0, 0, 0); }", "SEC004"),
    ("void f(void) { char *p = malloc(4); }", "SEC005"),
    ('void f(void) { __asm__ volatile ("nop"); }', "SEC006"),
    ("void f(void) { *(volatile uint32_t *)0x40014000 = 1; }", "SEC007"),
    ("void f(void) { sleep_ms(500); }", "SEC008"),
    ("void f(void) { while (1) { } }", "SEC009"),
    ("void f(void) { for (;;) { } }", "SEC009"),
    ("int f(const char *a) { return atoi(a); }", "SEC010"),
    ("void f(int n) { char buf[n]; }", "SEC011"),
    ("void f(void) { char big[4096]; big[0] = 0; }", "SEC012"),
])
def test_each_rule_finds_its_pattern(line, rule):
    assert rule in [r for r, _ in rules(line)], rules(line)


def test_comments_and_strings_do_not_count_but_lines_stay_right():
    text = "// printf(\"x\");\n/* gpio_put(13,1);\n */\nvoid f(void) {\n  const char *s = \"strcpy(\";\n  gpio_put(12, 0);\n}\n"
    assert rules(text) == [("SEC001", 6)]
    stripped = strip_code(text)
    assert stripped.count("\n") == text.count("\n") and "printf" not in stripped


def test_small_sleeps_and_static_buffers_are_fine():
    assert rules("void f(void) { sleep_ms(2); }") == []
    assert rules("static char big[4096];") == []                 # static, not on the stack


def test_verdict_and_counts():
    r = scan_sources({"a.c": "void f(void) { gpio_put(13,1); }", "b.c": "int g(const char *a) { return atoi(a); }"})
    assert r["verdict"] == "FAIL" and r["counts"]["critical"] == 1 and r["counts"]["low"] == 1
    assert r["findings"][0]["severity"] == "critical" and r["findings"][0]["file"] == "a.c"
    assert r["findings"][0]["function"] == "f"
    assert ("SEC013", 0) in rules("int x;", keep_info=True)                      # app_command missing (info only)
    assert scan_sources({"x.c": "int x;"})["verdict"] == "PASS"


# ------------------------------------------------------------------ through the tools
def call(client, name, args, role="agent"):
    return client.post(f"/tools/{name}/call", headers=auth(role), json={"arguments": args})


def test_scan_tool_on_a_project_and_on_a_build(client, services):
    client.post("/projects", headers=auth("agent"), json={"name": "demo"})
    client.put("/projects/demo/sources/bad.c", headers=auth("agent"), json={"content": "void f(void){ gpio_put(13,1); }"})
    r = call(client, "security_scan", {"project": "demo"}, "viewer").json()["result"]
    assert r["verdict"] == "FAIL" and r["target"] == {"project": "demo"} and "bad.c" in r["files"]
    add_build(services, 1, files={"app.c": GOOD})
    r = call(client, "security_scan", {"build_id": 1}, "viewer").json()["result"]
    assert r["verdict"] == "PASS" and r["files"] == ["app.c"]
    assert call(client, "security_scan", {}, "viewer").status_code == 422
    assert call(client, "security_scan", {"project": "demo", "build_id": 1}, "viewer").status_code == 422
    assert call(client, "security_scan", {"build_id": 99}, "viewer").status_code == 404


def test_size_report_tool(client, services):
    add_build(services, 1)
    add_build(services, 2, seed=2, files={"app.c": GOOD})
    r = call(client, "size_report", {"build_id": 2}, "viewer").json()["result"]
    assert r["build_id"] == 2 and r["compared_to"]["build_id"] == 1 and r["compared_to"]["flash_delta"] == 0
    assert r["symbols_available"] is False                         # the test build has no real ELF
    assert call(client, "size_report", {"build_id": 1}, "viewer").json()["result"]["compared_to"] is None
    add_build(services, 3, status=BUILD_FAILED)
    assert call(client, "size_report", {"build_id": 3}, "viewer").status_code == 409


def test_latency_tool(client):
    r = call(client, "measure_latency", {"command": "PING", "count": 5}).json()["result"]
    assert r["count"] == 5 and r["errors"] == 0 and r["replies"] == ["OK PONG"]
    assert r["min_ms"] <= r["median_ms"] <= r["p95_ms"] <= r["max_ms"]
    assert FakeLink.sent.count("PING") == 5
    assert call(client, "measure_latency", {"command": "BOOTSEL", "count": 2}).status_code in (403, 422)
    assert call(client, "measure_latency", {"command": "PING", "count": 99}).status_code == 422
    assert call(client, "measure_latency", {"command": "PING"}, "viewer").status_code == 403
    assert "BOOTSEL" not in FakeLink.sent
