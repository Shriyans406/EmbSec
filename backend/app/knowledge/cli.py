"""The knowledge base from a terminal.   (from the backend folder)

  python -m app.knowledge.cli collect                  read every record into the knowledge base
  python -m app.knowledge.cli search "division hang"   full-text search  (--kind bug --project calc)
  python -m app.knowledge.cli related "write a serial parser" --project thermo    what an agent would be told
  python -m app.knowledge.cli timeline calc2           the project's history
  python -m app.knowledge.cli board DE61C48787331A35   which firmware a board ran when
  python -m app.knowledge.cli stats
"""
from __future__ import annotations

import argparse
import sys

from ..db import migrate
from ..settings import Settings
from .history import board_history, timeline
from .service import KnowledgeService


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.knowledge.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect")
    sub.add_parser("stats")
    s = sub.add_parser("search")
    s.add_argument("text")
    s.add_argument("--kind")
    s.add_argument("--project")
    r = sub.add_parser("related")
    r.add_argument("text")
    r.add_argument("--project")
    t = sub.add_parser("timeline")
    t.add_argument("project")
    b = sub.add_parser("board")
    b.add_argument("uid")
    args = ap.parse_args(argv)
    db = Settings().db_path
    migrate(db)
    k = KnowledgeService(db)
    if args.cmd == "collect":
        print(k.collect())
    elif args.cmd == "stats":
        k.collect()
        print(k.store.stats())
    elif args.cmd == "search":
        for h in k.search(args.text, args.kind, args.project, 20):
            print(f"  #{h['id']:<4} {h['kind']:<13} {str(h['project'] or '-'):<10} {h['score']:>6}  {h['title']}")
            print(f"        {h['snippet']}")
    elif args.cmd == "related":
        print(k.context_for(args.text, args.project)[0] or "(nothing relevant yet)")
    elif args.cmd == "timeline":
        for e in timeline(db, args.project):
            print(f"  {e['ts']}  {e['type']:<14} {e['summary']}")
    elif args.cmd == "board":
        h = board_history(db, args.uid)
        for p in h["firmware_periods"]:
            print(f"  {p['from']} .. {p['until'] or 'now':<25} build #{p['build_id']} ({p['project']}) "
                  f"approved by {p['approved_by']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
