"""Security and safety scanner for AEISP application code (plain C, app/ folder only).

It is a rule-based checker, not a compiler: it strips comments and string literals, then looks for patterns that
are dangerous on this board or in this harness. Every finding has a rule id, a severity, the file and line, and
advice. The verdict is FAIL when there is any critical or high finding.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List

SEVERITIES = ["critical", "high", "medium", "low", "info"]
BLOCKING = {"critical", "high"}


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    pattern: str
    advice: str


RULES: List[Rule] = [
    Rule("SEC001", "critical", "touches GPIO 12/13 (FPGA power / enable)",
         r"\bgpio_\w+\s*\(\s*(?:12|13)\s*[,)]|\b1u?l?\s*<<\s*(?:12|13)\b|\bGPIO_?(?:12|13)\b",
         "never drive or configure GPIO 12 or 13 on the Shrike Lite"),
    Rule("SEC002", "high", "uses USB stdio (belongs to the harness protocol)",
         r"\b(?:printf|puts|putchar|getchar|getchar_timeout_us|scanf|fgets|stdio_init_all|stdio_usb_init)\s*\(",
         "return text through app_command's out buffer instead"),
    Rule("SEC003", "high", "unbounded string function",
         r"\b(?:strcpy|strcat|sprintf|vsprintf|gets|stpcpy)\s*\(",
         "use snprintf / strncpy with the buffer size"),
    Rule("SEC004", "high", "reaches the bootloader, flash, watchdog or second core",
         r"\b(?:reset_usb_boot|rom_func_lookup|flash_range_erase|flash_range_program|watchdog_enable|"
         r"watchdog_reboot|watchdog_update|multicore_launch_core1|multicore_reset_core1)\s*\(|"
         r"#\s*include\s*[<\"](?:hardware/flash|pico/bootrom|hardware/watchdog|pico/multicore)\.h",
         "the harness owns these; application code must not use them"),
    Rule("SEC005", "high", "heap allocation",
         r"\b(?:malloc|calloc|realloc|free)\s*\(",
         "use static buffers; there is no heap budget for app code"),
    Rule("SEC006", "high", "inline assembly", r"\b(?:__asm__|__asm|asm)\s*(?:volatile\s*)?\(",
         "write it in C"),
    Rule("SEC007", "medium", "raw peripheral register access",
         r"\*\s*\(\s*volatile\s+\w+\s*\*\s*\)\s*0x[0-9a-fA-F]+|\b(?:hw_set_bits|hw_clear_bits|hw_write_masked)\s*\(",
         "use the SDK's gpio / adc functions"),
    Rule("SEC008", "medium", "long blocking delay", r"\bsleep_ms\s*\(\s*(?:[5-9]\d|\d{3,})\s*\)|\bbusy_wait_\w+\s*\(",
         "keep app_tick and app_command short; use time stamps instead of waiting"),
    Rule("SEC009", "medium", "endless loop", r"\bwhile\s*\(\s*(?:1|true)\s*\)|\bfor\s*\(\s*;\s*;\s*\)",
         "the harness calls app_tick in its own loop; an endless loop trips the 3 s watchdog"),
    Rule("SEC010", "low", "atoi has no error checking", r"\batoi\s*\(", "use strtol and check the end pointer"),
    Rule("SEC011", "low", "variable-length array", r"\b(?:char|int|uint\w*|int\w*_t)\s+\w+\s*\[\s*[a-z_]\w*\s*\]\s*;",
         "use a fixed-size buffer"),
]

_LARGE_ARRAY = re.compile(r"\b(?:char|int|uint\w*|int\w*_t|float|double)\s+\w+\s*\[\s*(\d+)\s*\]")
_FUNC = re.compile(r"^[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;{]*\)\s*\{", re.M)


def strip_code(text: str) -> str:
    """Blank out comments and string / char literals but keep every newline, so line numbers stay right."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        at_line_start = i == 0 or text[i - 1] == "\n"
        if at_line_start and re.match(r"[ \t]*#[ \t]*include\b", text[i:i + 40]):
            j = text.find("\n", i)                  # keep include lines as they are: the path matters
            j = n if j < 0 else j
            line = text[i:j]
            cut = line.find("//")
            out.append(line if cut < 0 else line[:cut] + " " * (len(line) - cut))
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c + " " * max(0, j - i - 2) + (c if j - i >= 2 else ""))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _function_at(code: str, pos: int) -> str:
    name = ""
    for m in _FUNC.finditer(code):
        if m.start() > pos:
            break
        name = m.group(1)
    return name


def scan_sources(files: Dict[str, str]) -> dict:
    findings: List[dict] = []
    defined = set()
    for name in sorted(files):
        raw = files[name]
        code = strip_code(raw)
        lines = raw.splitlines()
        defined.update(m.group(1) for m in _FUNC.finditer(code))
        for rule in RULES:
            for m in re.finditer(rule.pattern, code):
                ln = _line_of(code, m.start())
                if rule.id == "SEC009" and not _function_at(code, m.start()):
                    continue
                findings.append({"rule": rule.id, "severity": rule.severity, "title": rule.title, "file": name,
                                 "line": ln, "code": lines[ln - 1].strip()[:160] if ln <= len(lines) else "",
                                 "function": _function_at(code, m.start()), "advice": rule.advice})
        for m in _LARGE_ARRAY.finditer(code):
            size = int(m.group(1))
            fn = _function_at(code, m.start())
            depth = code[:m.start()].count("{") - code[:m.start()].count("}")
            if size >= 1024 and fn and depth > 0:
                ln = _line_of(code, m.start())
                findings.append({"rule": "SEC012", "severity": "medium", "title": f"{size}-byte array on the stack",
                                 "file": name, "line": ln, "code": lines[ln - 1].strip()[:160], "function": fn,
                                 "advice": "make it static or smaller: the stack is only a few KiB"})
    if files and "app_command" not in defined:
        findings.append({"rule": "SEC013", "severity": "info", "title": "app_command is not defined",
                         "file": "", "line": 0, "code": "", "function": "",
                         "advice": "without it every APP command answers with the default error"})
    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["file"], f["line"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    verdict = "FAIL" if any(counts[s] for s in BLOCKING) else "PASS"
    return {"verdict": verdict, "counts": counts, "files": sorted(files), "findings": findings,
            "rules": len(RULES) + 2}


def rule_table() -> List[dict]:
    return [asdict(r) for r in RULES]
