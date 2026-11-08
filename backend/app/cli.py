"""Backend setup tools.   Run from the backend folder:  python -m app.cli <command>

  make-tokens     create the API tokens in the project's .env file (only the ones that are missing)
  show-tokens     print the API tokens (they are secrets: do not paste them anywhere public)
  db-info         show the database file, its schema version and table sizes
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys

from .db import migrate
from .security import new_token
from .settings import ROOT, Settings

WANTED = ["AEISP_OPERATOR_TOKEN", "AEISP_AGENT_TOKEN", "AEISP_VIEWER_TOKEN"]
ROLE_OF = {"AEISP_OPERATOR_TOKEN": "operator", "AEISP_AGENT_TOKEN": "agent", "AEISP_VIEWER_TOKEN": "viewer"}


def cmd_make_tokens(args) -> int:
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    created = {}
    for key in WANTED:
        pat = re.compile(rf"^\s*{key}\s*=\s*(.*)$")
        idx = next((i for i, l in enumerate(lines) if pat.match(l)), None)
        if idx is not None and pat.match(lines[idx]).group(1).strip():
            continue  # already has a value
        tok = new_token()
        created[key] = tok
        if idx is None:
            lines.append(f"{key}={tok}")
        else:
            lines[idx] = f"{key}={tok}"
    if not created:
        print("All three tokens already exist in .env. Use  python -m app.cli show-tokens  to read them.")
        return 0
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(created)} new token(s) to {env}\n")
    for key, tok in created.items():
        print(f"  {ROLE_OF[key]:<9} {tok}")
    print("\nThese are secrets. .env is excluded from git. Restart the server so it picks them up.")
    return 0


def cmd_show_tokens(args) -> int:
    s = Settings()
    for role, tok in (("operator", s.operator_token), ("agent", s.agent_token), ("viewer", s.viewer_token)):
        print(f"  {role:<9} {tok or '(not set)'}")
    return 0


def cmd_db_info(args) -> int:
    s = Settings()
    v = migrate(s.db_path)
    print(f"database : {s.db_path}")
    print(f"schema   : version {v}")
    c = sqlite3.connect(s.db_path)
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<20} {n:>6} rows")
    c.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("make-tokens").set_defaults(fn=cmd_make_tokens)
    sub.add_parser("show-tokens").set_defaults(fn=cmd_show_tokens)
    sub.add_parser("db-info").set_defaults(fn=cmd_db_info)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())