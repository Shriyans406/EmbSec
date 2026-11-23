"""Versioned prompts.

Each prompt is a Markdown file in app/ai/prompt_files/ with a small header:

    ---
    name: lab_agent
    version: 1
    purpose: ...
    ---
    text ...

`$name` placeholders in the text are filled with string.Template (so C code with braces is safe). Every run
stores the name, version and sha256 of the prompts it used, so a result can always be traced to its prompt.
"""
from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

PROMPT_DIR = Path(__file__).with_name("prompt_files")


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    purpose: str
    text: str
    sha256: str

    def render(self, **values) -> str:
        try:
            return string.Template(self.text).substitute(values)
        except KeyError as e:
            raise PromptError(f"prompt {self.name} needs a value for {e}") from e

    def ref(self) -> dict:
        return {"name": self.name, "version": self.version, "sha256": self.sha256[:12]}


def parse(text: str, source: str = "?") -> Prompt:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise PromptError(f"{source}: a prompt starts with a '---' header")
    try:
        head, body = text[4:].split("\n---\n", 1)
    except ValueError as e:
        raise PromptError(f"{source}: the header is not closed with '---'") from e
    meta: Dict[str, str] = {}
    for line in head.splitlines():
        if line.strip():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    for key in ("name", "version"):
        if key not in meta:
            raise PromptError(f"{source}: the header has no '{key}'")
    try:
        version = int(meta["version"])
    except ValueError as e:
        raise PromptError(f"{source}: version must be a whole number") from e
    body = body.strip() + "\n"
    return Prompt(meta["name"], version, meta.get("purpose", ""), body, hashlib.sha256(body.encode()).hexdigest())


@lru_cache(maxsize=None)
def load(name: str) -> Prompt:
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptError(f"no prompt named {name!r}")
    p = parse(path.read_text(encoding="utf-8"), path.name)
    if p.name != name:
        raise PromptError(f"{path.name}: the header says name {p.name!r}")
    return p


def available() -> List[dict]:
    return [load(p.stem).ref() | {"purpose": load(p.stem).purpose} for p in sorted(PROMPT_DIR.glob("*.md"))]
