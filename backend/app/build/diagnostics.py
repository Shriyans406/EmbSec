"""Turns raw compiler / linker / CMake output into structured diagnostics."""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

# file:line[:col]: severity: message          (GCC; the optional drive letter keeps C:\ paths intact)
_GCC = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:\r\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<sev>fatal error|error|warning|note):\s*(?P<msg>.*?)\s*$"
)
# Linker, as printed by arm-none-eabi-ld on Windows:
#   ...\ld.exe: CMakeFiles/x.dir/.../app_src/app.c.obj: in function `app_init':
#   app.c:(.text.app_init+0x2): undefined reference to `missing_function'
_LINK_FUNC = re.compile(r"^.*[\\/]ld(?:\.exe)?: (?P<obj>\S+\.obj): in function [`'](?P<fn>[^'`]+)'")
_LINK_REF = re.compile(
    r"^(?P<file>[^:\r\n]+?):\((?P<section>[^)]*)\):\s*"
    r"(?P<msg>(?:undefined reference to|multiple definition of).*?)\s*$"
)
# tool: error: message     e.g.  collect2.exe: error: ld returned 1 exit status
_TOOL = re.compile(r"^(?P<tool>[\w.+\-]+): (?P<sev>fatal error|error|warning): (?P<msg>.*?)\s*$")
# ...\ld.exe: region `FLASH' overflowed by 8 bytes
_LD = re.compile(r"^.*[\\/]ld(?:\.exe)?: (?P<msg>.+?)\s*$")
_CMAKE = re.compile(r"^CMake (?P<sev>Error|Warning)(?P<rest>.*)$")
_FLAG = re.compile(r"\s+\[(-W[^\]]+)\]$")
_CARET = re.compile(r"^\s*(?:\d+\s*)?\|")


@dataclass
class Diagnostic:
    severity: str                 # "error" | "warning" | "note"
    message: str
    file: Optional[str] = None    # display path, e.g. "app/app.c"
    line: Optional[int] = None
    column: Optional[int] = None
    flag: Optional[str] = None    # e.g. "-Wunused-variable"
    source: str = "other"         # "app" | "harness" | "sdk" | "toolchain" | "cmake" | "policy" | "other"
    category: str = "compile"     # "compile" | "link" | "cmake" | "policy" | "other"
    context: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        where = ""
        if self.file:
            where = self.file
            if self.line is not None:
                where += f":{self.line}"
                if self.column is not None:
                    where += f":{self.column}"
            where += ": "
        flag = f" [{self.flag}]" if self.flag else ""
        return f"{self.severity}: {where}{self.message}{flag}"


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).replace("\\", "/")


def _classify(file: str, app_dir: Path, harness_dir: Path, sdk_dir: Path):
    """Return (display_path, source) for a path printed by the toolchain."""
    n = _norm(file)
    for base, label, src in (
        (app_dir, "app", "app"),
        (harness_dir, "harness", "harness"),
        (sdk_dir, "sdk", "sdk"),
    ):
        b = _norm(str(base))
        if n == b or n.startswith(b + "/"):
            rel = n[len(b):].lstrip("/")
            # keep the original capitalisation of the file name part
            orig = file.replace("\\", "/")
            idx = orig.lower().rfind(rel.lower()) if rel else -1
            rel_disp = orig[idx:] if idx >= 0 else rel
            return (f"{label}/{rel_disp}" if rel_disp else label), src
    return file.replace("\\", "/"), "other"


def _classify_object(obj: str):
    """Work out which source file an object path from a linker message came from."""
    o = obj.replace("\\", "/")
    if "app_src/" in o:
        return "app/" + o.split("app_src/")[-1][: -len(".obj")], "app"
    marker = "aeisp_firmware.dir/"
    if marker in o:
        rest = o.split(marker, 1)[1]
        if rest.startswith("src/"):
            return "harness/" + rest[: -len(".obj")], "harness"
    return o[: -len(".obj")] if o.endswith(".obj") else o, "sdk" if "pico-sdk" in o.lower() else "other"


def parse_output(text: str, app_dir: Path, harness_dir: Path, sdk_dir: Path) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    seen = set()
    lines = text.splitlines()
    cur_obj = ""
    cur_fn = ""

    def add(d: Diagnostic) -> Diagnostic:
        key = (d.severity, d.file, d.line, d.column, d.message)
        if key in seen:
            return d
        seen.add(key)
        diags.append(d)
        return d

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        i += 1
        m = _GCC.match(raw)
        if m:
            sev = "error" if m.group("sev") == "fatal error" else m.group("sev")
            msg = m.group("msg")
            flag = None
            fm = _FLAG.search(msg)
            if fm:
                flag = fm.group(1)
                msg = msg[: fm.start()]
            disp, src = _classify(m.group("file"), app_dir, harness_dir, sdk_dir)
            d = Diagnostic(
                severity=sev, message=msg, file=disp, line=int(m.group("line")),
                column=int(m.group("col")) if m.group("col") else None,
                flag=flag, source=src, category="compile",
            )
            ctx = []
            while i < len(lines) and _CARET.match(lines[i]) and len(ctx) < 6:
                ctx.append(lines[i].rstrip())
                i += 1
            d.context = ctx
            add(d)
            continue

        m = _LINK_FUNC.match(raw)
        if m:
            cur_obj, cur_fn = m.group("obj"), m.group("fn")
            continue

        m = _LINK_REF.match(raw)
        if m:
            disp, src = _classify_object(cur_obj) if cur_obj else (m.group("file"), "other")
            msg = m.group("msg")
            if cur_fn:
                msg += f" (in function `{cur_fn}')"
            add(Diagnostic("error", msg, disp, None, None, None, src, "link"))
            continue

        m = _TOOL.match(raw)
        if m:
            sev = "error" if m.group("sev") == "fatal error" else m.group("sev")
            add(Diagnostic(sev, f"{m.group('tool')}: {m.group('msg')}", None, None, None, None,
                           "toolchain", "link" if "ld returned" in m.group("msg") else "other"))
            continue

        m = _LD.match(raw)
        if m and "in function" not in m.group("msg"):
            msg = m.group("msg")
            sev = "warning" if msg.startswith("warning:") else "error"
            add(Diagnostic(sev, msg.replace("warning: ", "", 1), None, None, None, None, "toolchain", "link"))
            continue

        m = _CMAKE.match(raw)
        if m:
            body = []
            while i < len(lines) and len(body) < 3:
                nxt = lines[i].strip()
                i += 1
                if not nxt:
                    if body:
                        break
                    continue
                body.append(nxt)
            sev = "error" if m.group("sev") == "Error" else "warning"
            msg = " ".join(body) if body else raw.strip()
            add(Diagnostic(sev, msg, None, None, None, None, "cmake", "cmake"))
            continue
    return diags


def count(diags: List[Diagnostic]):
    errors = sum(1 for d in diags if d.severity == "error")
    warnings = sum(1 for d in diags if d.severity == "warning")
    return errors, warnings 