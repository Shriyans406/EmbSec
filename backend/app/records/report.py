"""Reports of task records: Markdown for reading and diffing, PDF for handing over, JSON (the record) for machines.

The PDF writer is deliberately small and has no dependencies: A4 pages, Helvetica / Helvetica-Bold / Courier, text
wrapped to the page, a header and page numbers. It understands the Markdown this module writes (headings, lists,
tables, code blocks), nothing more.
"""
from __future__ import annotations

import textwrap
from typing import List

from .record import timeline

SUMMARY_KEYS = {
    "pipeline": ("requirement", "project", "status", "outcome", "build_id", "error"),
    "remediation": ("finding_id", "project", "status", "outcome", "before_build", "after_build", "root_cause", "error"),
    "optimisation": ("project", "goal", "status", "outcome", "base_build", "variant_build", "error"),
    "agent_run": ("agent", "goal", "status", "outcome", "steps", "tokens", "error"),
    "fuzz_campaign": ("project", "status", "build_id", "inputs_sent", "anomalies", "resets", "findings", "stop_reason"),
}


def _cell(v) -> str:
    return str(v if v is not None else "-").replace("|", "/").replace("\n", " ")[:90]


def _table(head: List[str], rows: List[list]) -> List[str]:
    if not rows:
        return ["(none)", ""]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out + [""]


def to_markdown(rec: dict, verification: dict = None, timeline_limit: int = 80) -> str:
    t = rec["task"]
    L = [f"# {rec['kind'].replace('_', ' ').title()} {rec['id']}", "",
         f"Record taken {rec['taken_at']}, window {rec['window']['from']} .. {rec['window']['until']}.  ",
         f"Record hash `{rec['record_hash'][:16]}`, audit head #{(rec.get('audit_head') or {}).get('id')} "
         f"`{((rec.get('audit_head') or {}).get('hash') or '')[:16]}`.", "", "## Summary", ""]
    for k in SUMMARY_KEYS.get(rec["kind"], ()):
        if t.get(k) not in (None, ""):
            L.append(f"- **{k}:** {str(t[k])[:600]}")
    L += ["", f"Model tokens: {rec['tokens']}. " + ", ".join(f"{v} {k}" for k, v in rec["counts"].items()) + ".", ""]
    L += ["## Agents", ""] + _table(["run", "agent", "status", "outcome", "steps", "model calls", "tokens"], [
        [a["id"], a["agent"], a["status"], a["outcome"], a["steps"], len(a["llm_calls"]),
         sum(c.get("total_tokens") or 0 for c in a["llm_calls"])] for a in rec["agents"]])
    L += ["## Jobs", ""] + _table(["job", "kind", "status", "requested by", "approved by", "summary"], [
        [j["id"], j["kind"], j["status"], j["requested_by"], j.get("approved_by"), j.get("summary")] for j in rec["jobs"]])
    L += ["## Builds", ""] + _table(["build", "project", "status", "harness", "flash", "RAM", "sources", "image"], [
        [b["id"], b["project"], b["status"], b["result"].get("harness_version"), b["result"].get("flash_bytes"),
         b["result"].get("ram_bytes"), (b["result"].get("sources_sha256") or "")[:8], (b["result"].get("uf2_sha256") or "")[:12]]
        for b in rec["builds"]])
    L += ["## Deployments and approvals", ""] + _table(["deployment", "build", "status", "board", "approved by"], [
        [d["id"], d["build_id"], d["status"], d.get("board_uid"), d.get("approved_by")] for d in rec["deployments"]])
    if rec["approvals"]:
        L += _table(["proposal job", "status", "approved by", "what"], [
            [a["job_id"], a["status"], a.get("approved_by"), a["summary"]] for a in rec["approvals"]])
    L += ["## Tests", ""] + _table(["run", "name", "passed", "total", "build"], [
        [x["id"], x["name"], x["passed"], x["total"], x.get("build_id")] for x in rec["test_runs"]])
    if rec["benchmark_runs"]:
        L += ["## Benchmarks", ""] + _table(["run", "build", "repeatable", "flash", "RAM"], [
            [b["id"], b["build_id"], bool(b["repeatable"]), b.get("flash_bytes"), b.get("ram_bytes")] for b in rec["benchmark_runs"]])
    if rec["fuzz_findings"]:
        L += ["## Fuzz findings", ""] + _table(["finding", "severity", "kind", "input", "status"], [
            [f["id"], f["severity"], f["kind"], f["minimized"] or f["input_text"], f["status"]] for f in rec["fuzz_findings"]])
    if verification:
        L += ["## Verification", "", f"Result: **{'OK' if verification['ok'] else 'FAILED'}** "
              f"({verification['failed']} failed, {verification['warnings']} warnings).", ""]
        L += _table(["check", "result", "detail"], [[c["check"], c["status"], c["detail"]] for c in verification["checks"]])
    tl = timeline(rec)
    L += ["## Timeline", "", f"{len(tl)} events" + (f", the first {timeline_limit} shown" if len(tl) > timeline_limit else "")
          + ":", ""]
    L += [f"- {e['ts'][:19].replace('T', ' ')} {e['source']}: {e['what'][:140]}" for e in tl[:timeline_limit]]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- PDF
def _pdf_text(s: str) -> str:
    s = s.encode("cp1252", errors="replace").decode("cp1252")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def to_pdf(markdown: str, title: str = "AEISP report") -> bytes:
    W, H, M = 595, 842, 48                       # A4 in points, margin
    lines: List[tuple] = []                      # (font, size, text, indent)
    in_code = False
    for raw in markdown.splitlines():
        if raw.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            for part in textwrap.wrap(raw, 95) or [""]:
                lines.append(("F3", 7.5, part, 0))
            continue
        if raw.startswith("# "):
            lines += [("F2", 16, raw[2:], 0), ("F1", 4, "", 0)]
        elif raw.startswith("## "):
            lines += [("F1", 6, "", 0), ("F2", 12, raw[3:], 0)]
        elif raw.startswith("|"):
            if set(raw.replace("|", "").strip()) <= {"-"}:
                continue
            cells = [c.strip() for c in raw.strip("|").split("|")]
            lines.append(("F3", 7, "  ".join(c[:28].ljust(min(28, max(6, len(c)))) for c in cells)[:118], 0))
        else:
            text = raw.replace("**", "").replace("`", "")
            indent = 10 if text.startswith("- ") else 0
            for i, part in enumerate(textwrap.wrap(text, 100) or [""]):
                lines.append(("F1", 9, part, indent if i == 0 else indent + 8))
    pages, page, y = [], [], H - M - 20
    for font, size, text, indent in lines:
        step = size + 3
        if y - step < M:
            pages.append(page)
            page, y = [], H - M - 20
        y -= step
        if text:
            page.append(f"BT /{font} {size} Tf {M + indent} {y:.1f} Td ({_pdf_text(text)}) Tj ET")
    pages.append(page)

    objs: List[bytes] = []

    def add(body: str) -> int:
        objs.append(body.encode("cp1252", errors="replace"))
        return len(objs)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("PAGES")
    fonts = [add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"),
             add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"),
             add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")]
    kids = []
    for n, ops in enumerate(pages, 1):
        head = [f"BT /F1 7 Tf {M} {H - M + 8} Td ({_pdf_text(title)}) Tj ET",
                f"BT /F1 7 Tf {W - M - 60} {M - 20} Td (page {n} of {len(pages)}) Tj ET"]
        stream = "\n".join(head + ops)
        content = add(f"<< /Length {len(stream.encode('cp1252', errors='replace'))} >>\nstream\n{stream}\nendstream")
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Contents {content} 0 R "
                        f"/Resources << /Font << /F1 {fonts[0]} 0 R /F2 {fonts[1]} 0 R /F3 {fonts[2]} 0 R >> >> >>"))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info << /Title ({_pdf_text(title)}) >> >>\n" \
           f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
