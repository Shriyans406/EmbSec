"""tools/acceptance.py stays in step with the test suite and the documents."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location("aeisp_acceptance", ROOT / "tools" / "acceptance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_test_file_backs_an_item_and_every_named_file_exists():
    acc = load()
    named = {f for *_, files in acc.ITEMS for f in files}
    present = {p.stem for p in (ROOT / "backend" / "tests").glob("test_*.py")}
    assert named - present == set(), "the checklist names test files that do not exist"
    assert present - named == set(), "a test file is not part of any acceptance item"


def test_every_item_is_documented():
    acc = load()
    doc = (ROOT / "docs" / "acceptance.md").read_text(encoding="utf-8")
    for code, *_ in acc.ITEMS:
        assert f"| {code} |" in doc, code


def test_env_parsing_and_the_key_pattern(tmp_path):
    import re

    acc = load()
    env = tmp_path / ".env"
    env.write_text("﻿# c\nAEISP_AGENT_TOKEN='abc'\nX = 1\n", encoding="utf-8")
    assert acc.read_env(env) == {"AEISP_AGENT_TOKEN": "abc", "X": "1"}
    assert re.search(acc.KEY_PATTERN, "AIza" + "B" * 35) and not re.search(acc.KEY_PATTERN, "AIza-short")
